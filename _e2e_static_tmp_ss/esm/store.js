/* esm/store.js —— 单一状态对象（迭代四 F2⑤）。
 * subscribe/setState；notifyLog（F3 通知历史）挂这里。
 * 打点 [store]：setState 只打改动的键名、不含值（防日志泄漏内容）。
 */
const listeners = new Set();

export const store = {
  state: { notifyLog: [], canvasState: null },

  subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },

  setState(patch) {
    const keys = Object.keys(patch);
    Object.assign(this.state, patch);
    // [store] 打点：只打键名，不打值
    try { console.info("[store] setState:", keys.join(",")); } catch (e) { /* 零阻断 */ }
    for (const fn of listeners) {
      try { fn(this.state, keys); } catch (e) { /* 订阅者异常零阻断 */ }
    }
  },
};
