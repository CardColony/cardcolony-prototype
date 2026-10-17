/* esm/flow/submission-policy.js —— 提交策略（dsh `input/submission-policy.ts` 移植）
 *
 * 出处：deepseek-harness/packages/client/ui-conversation/src/client/input/submission-policy.ts
 * 移植口径：`resolve()` 的判据逐行对应（:48-57）；`setBusyEnter()` 的
 *   「先发活值、再落持久化」顺序保留（:64-68）。
 *
 * 本机改动（仅替换外部依赖）：
 *   · dsh 的 `createSnapshotStore` 来自 client-runtime；本机没有那层运行时，
 *     故在本文件内给一个同形最小实现（getSnapshot / set / subscribe）。
 *     这是**载体替换**，不是机制改动：策略只用到这三个方法。
 *   · dsh 的 `SettingsScope`（持久化偏好作用域）在本机不存在 ⇒ 构造参数 host
 *     可省略；给了就按原文 adopt 逻辑订阅，不给＝进程内偏好（原文 :33 的注释
 *     已声明「absent compositions stay process-local」）。
 */
import { DEFAULT_BUSY_ENTER_BEHAVIOR } from "./composer-submission.js?v=1";

/** dsh client-runtime `createSnapshotStore` 的同形最小实现（get/set/subscribe）。 */
export function createSnapshotStore(initial) {
  let value = initial;
  const listeners = new Set();
  return {
    getSnapshot: () => value,
    set(next) {
      if (next === value) return;
      value = next;
      for (const fn of Array.from(listeners)) {
        try { fn(); } catch (e) { /* 订阅者异常不阻断其余订阅者 */ }
      }
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => { listeners.delete(fn); };
    },
  };
}

/**
 * 输入条与其设置行共用的忙时 Enter 策略（原文 :22-79）。
 * 直接 `steer` 是尽力而为：窗口关闭时由投递侧转成下一次可唤醒的排队项。
 */
export class ComposerSubmissionPolicy {
  constructor(host) {
    /** 设置行的响应式偏好源。 */
    this.busyEnter = createSnapshotStore(DEFAULT_BUSY_ENTER_BEHAVIOR);
    this.host = host;
    if (host !== undefined) {
      host.subscribe(() => { this.adopt(host); });
      this.adopt(host);
    }
  }

  /**
   * 不改状态地解析一次键盘手势（原文 :48-57）。
   * @param {boolean} running 被寻址的 agent 是否忙
   * @param {"enter"|"accelerated"} gesture 普通 Enter 或 Cmd/Ctrl 加速和弦
   * @param {boolean} steeringAvailable 本会话通道是否支持 steer
   * @returns {"queue"|"steer"}
   */
  resolve(running, gesture, steeringAvailable) {
    if (!running || !steeringAvailable) return "queue";
    const preferred = this.busyEnter.getSnapshot();
    if (gesture === "enter") return preferred;
    return preferred === "queue" ? "steer" : "queue";
  }

  /** 改忙时普通 Enter 的行为：活值先发，再起持久化写（原文 :64-68）。 */
  setBusyEnter(behavior) {
    if (this.busyEnter.getSnapshot() === behavior) return;
    this.busyEnter.set(behavior);
    if (this.host && typeof this.host.set === "function") this.host.set("busyEnter", behavior);
  }

  /** 采用作用域里已被接受的值，不写回（原文 :74-78）。 */
  adopt(host) {
    const section = host.getSnapshot().value;
    if (section === undefined || this.busyEnter.getSnapshot() === section.busyEnter) return;
    this.busyEnter.set(section.busyEnter);
  }
}
