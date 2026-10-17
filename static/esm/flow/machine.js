/* esm/flow/machine.js —— 输入状态机（dsh `input/machine.ts` 的移植件）
 *
 * 出处：deepseek-harness/packages/client/ui-conversation/src/client/input/machine.ts
 * 移植口径：**逐行对应**。相位集、草稿事务、撤销环、粘贴面、提交面、
 *   每一个事件分支与每一条早退判据，都按原文搬；只把 TS 类型换成 JS 约定。
 *
 * 相位集（原文 machine.ts:105-124 的字段 `phase`）：
 *   plain / claimed / adjudicating / submitting
 *   ★ 任务书 §一.1 的表格写作「idle / submitting / adjudicating」——那是摘要，
 *     原文实为四相位（`claimed` 是 /命令 token 认领态）。按「移植级照抄」取原文。
 *
 * 本机改动（仅类型层，无语义）：
 *   · `InputState['phase']` 等类型注解删除；`unreachable()` 保留为运行时兜底。
 *   · `AbortController` 用宿主全局（浏览器与 Node 18+ 均有）。
 *
 * 状态机的输出是**效果数组**（InputEffect），由 shell（input-bar.js / workspace.js）
 * 按序执行；机器本身不碰 DOM、不碰网络、不读时钟（时钟由 options.now 注入）。
 */

/** 草稿里每个引用位（chip）占位用的对象替换字符（原文 machine.ts:24）。 */
export const PLACEHOLDER = "\uFFFC";

/** 机器从不写队列；队列投影由接线层叠加（原文 machine.ts:27）。 */
const EMPTY_QUEUE = [];

/** 撤销环深度（原文 machine.ts:30）。 */
const LOG_LIMIT = 100;

/** 封闭事件/守卫联合的穷尽兜底（原文 machine.ts:33）。 */
function unreachable(value) {
  throw new Error("unreachable input event: " + JSON.stringify(value));
}

/**
 * 剥掉草稿上的命令 token，得到提交参数（原文 machine.ts:44-53）。
 * 容忍前导空白（含换行）；只吃一个分隔符，其余原样保留。
 */
function argsAfter(draft, token) {
  const s = draft.trimStart();
  if (s.startsWith(token)) return s.slice(token.length);
  const base = token.trimEnd();
  if (s.startsWith(base)) {
    const rest = s.slice(base.length);
    return /^\s/.test(rest) ? rest.slice(1) : rest;
  }
  return "";
}

/** 前后缀公共扫描求编辑区间（原文 machine.ts:59-67）。 */
function diffEdit(prev, next) {
  let p = 0;
  const maxCommon = Math.min(prev.length, next.length);
  while (p < maxCommon && prev[p] === next[p]) p += 1;
  let s = 0;
  const maxSuffix = maxCommon - p;
  while (s < maxSuffix && prev[prev.length - 1 - s] === next[next.length - 1 - s]) s += 1;
  return { start: p, end: prev.length - s, insertedLength: next.length - s - p };
}

/**
 * 把草稿里的占位符展开成各 occurrence 的剪贴板文本（原文 machine.ts:77-87）。
 * U+FFFC 永不出机器。
 */
export function projectClipboard(state) {
  const { draft, occurrences } = state;
  if (occurrences.length === 0) return draft;
  let out = "";
  let cursor = 0;
  for (const o of occurrences) {
    out += draft.slice(cursor, o.offset) + o.clipboardText;
    cursor = o.offset + 1;
  }
  return out + draft.slice(cursor);
}

/**
 * 纯输入状态机，每会话一个实例（原文 machine.ts:105-566）。
 * 每次 Enter 建一个 SubmitAttempt 与一个 AbortController，释放时自己 abort；
 * shell 只观察 attempt.signal，从不 abort。
 */
export class InputMachine {
  constructor(options = {}) {
    this.mergeWindowMs = options.mergeWindowMs ?? 1000;
    this.now = options.now ?? (() => 0);
    this.draft = "";
    this.draftRev = 0;
    this.phase = "plain";
    this.claim = undefined;
    this.occurrences = [];
    this.occurrenceSeq = 0;
    this.seq = 0;
    this.inflight = undefined;
    this.log = [];
    this.redoStack = [];
    this.typingRun = undefined;
    this.paste = undefined;
    this.pasteSeq = 0;
  }

  /** 机器状态的只读快照（队列在本层恒空；原文 machine.ts:132-144）。 */
  get state() {
    const c = this.claim;
    const out = {
      draft: this.draft,
      imageIds: [],
      draftRev: this.draftRev,
      phase: this.phase,
      occurrences: this.occurrences,
      queue: EMPTY_QUEUE,
    };
    if (c) out.claim = c.hint !== undefined ? { token: c.token, hint: c.hint } : { token: c.token };
    if (this.paste !== undefined) out.paste = this.paste;
    return out;
  }

  /** 喂一个事件（原文 machine.ts:151-174）：所有输入状态的唯一写路径。 */
  dispatch(ev) {
    switch (ev.type) {
      case "draft-changed": return this.onDraftChanged(ev.draft, ev.editRange);
      case "begin-command": return this.onBeginCommand(ev.claim, ev.span);
      case "insert-ref": return this.onInsertRef(ev.reference, ev.span);
      case "consume-token": return this.onConsumeToken(ev.guard);
      case "set-invalid": return this.onSetInvalid(ev.invalidIds);
      case "undo": return this.onUndo();
      case "redo": return this.onRedo();
      case "paste-begin": return this.onPasteBegin(ev.text, ev.selection, ev.components, ev.generation);
      case "paste-upgrade": return this.onPasteUpgrade(ev.attemptId, ev.span, ev.reference);
      case "invalidate-paste": {
        this.paste = undefined;
        return [];
      }
      case "enter": return this.onEnter(ev.mode);
      case "adjudicated": return this.onAdjudicated(ev.attempt, ev.outcome);
      case "adjudication-failed": return this.onAdjudicationFailed(ev.attempt, ev.message);
      case "submit-settled": return this.onSubmitSettled(ev);
      case "send-committed": return this.onSendCommitted();
      case "release": return this.onRelease();
      default: return unreachable(ev);
    }
  }

  // ---- 事务基建 ----

  /** 采用新草稿：推进 revision（span-CAS 的失效点；原文 :179-182）。 */
  adopt(draft) {
    this.draft = draft;
    this.draftRev += 1;
  }

  /** 压一个撤销单元（前置快照），修剪环，切断 redo 链（原文 :185-193）。 */
  pushTxn(selectionBefore) {
    const entry = { draftBefore: this.draft, occurrencesBefore: this.occurrences };
    if (selectionBefore !== undefined) entry.selectionBefore = selectionBefore;
    this.log.push(entry);
    if (this.log.length > LOG_LIMIT) this.log.shift();
    this.redoStack = [];
  }

  /** occurrence 表按一次编辑对账（旧草稿坐标；原文 :201-209）。 */
  reconcile(range) {
    const delta = range.insertedLength - (range.end - range.start);
    const kept = [];
    for (const o of this.occurrences) {
      if (o.offset < range.start) kept.push(o);
      else if (o.offset >= range.end) kept.push(delta === 0 ? o : Object.assign({}, o, { offset: o.offset + delta }));
    }
    this.occurrences = kept;
  }

  /** 认领完整性监视：破坏 token 前缀的编辑一律释放认领（原文 :212-217）。 */
  watchClaim() {
    if (this.phase === "claimed" && this.claim !== undefined && !this.draft.startsWith(this.claim.token)) {
      this.phase = "plain";
      this.claim = undefined;
    }
  }

  /** 在草稿偏移处铸一个 occurrence（原文 :220-230）。 */
  mint(reference, offset) {
    this.occurrenceSeq += 1;
    return {
      occurrenceId: this.occurrenceSeq,
      source: reference.source,
      ref: reference.ref,
      offset,
      label: reference.label,
      clipboardText: reference.clipboardText,
    };
  }

  /** 把新铸条目并入按偏移排序的表（原文 :233-236）。 */
  withMinted(minted) {
    if (minted.length === 0) return;
    this.occurrences = this.occurrences.concat(minted).sort((a, b) => a.offset - b.offset);
  }

  // ---- 草稿事务 ----

  /** 原文 :240-256 —— 任何相位都可编辑（无相位检查）。 */
  onDraftChanged(draft, editRange) {
    if (draft === this.draft) return [];
    const range = editRange ?? diffEdit(this.draft, draft);
    const typing = range.start === range.end && range.insertedLength === 1;
    const at = this.now();
    const run = this.typingRun;
    const merges = typing && run !== undefined && run.end === range.start && at - run.at <= this.mergeWindowMs;
    if (!merges) this.pushTxn({ start: range.start, end: range.end });
    this.typingRun = typing ? { end: range.start + 1, at } : undefined;
    this.reconcile(range);
    this.adopt(draft);
    this.watchClaim();
    this.paste = undefined;
    return [];
  }

  /** span CAS：revision 相等 ＋ 边界合理（原文 :259-262）。 */
  casOk(span) {
    return span.draftRev === this.draftRev
      && span.start >= 0 && span.start <= span.end && span.end <= this.draft.length;
  }

  /** 原文 :264-277 —— 前导触发契约：span 之前只允许空白。 */
  onBeginCommand(claim, span) {
    if (this.phase !== "plain" && this.phase !== "claimed") return [];
    if (!this.casOk(span) || this.draft.slice(0, span.start).trim() !== "") return [];
    this.pushTxn();
    this.typingRun = undefined;
    this.reconcile({ start: 0, end: span.end, insertedLength: claim.token.length });
    this.adopt(claim.token + this.draft.slice(span.end));
    this.claim = claim;
    this.phase = "claimed";
    this.paste = undefined;
    return [];
  }

  /** 原文 :279-285 */
  onInsertRef(reference, span) {
    if (this.phase !== "plain" && this.phase !== "claimed") return [];
    if (!this.casOk(span)) return [];
    this.replaceSpanWithChip(reference, span);
    this.paste = undefined;
    return [];
  }

  /** 共享的 chip 插入事务（原文 :293-304）。 */
  replaceSpanWithChip(reference, span) {
    this.pushTxn();
    this.typingRun = undefined;
    const tail = this.draft.slice(span.end);
    const gap = tail.length === 0 || tail[0] !== " " ? " " : "";
    const inserted = PLACEHOLDER + gap;
    this.reconcile({ start: span.start, end: span.end, insertedLength: inserted.length });
    this.withMinted([this.mint(reference, span.start)]);
    this.adopt(this.draft.slice(0, span.start) + inserted + tail);
    this.watchClaim();
    return inserted.length;
  }

  /** 业务成功后的带守卫 token 删除（原文 :311-337）。 */
  onConsumeToken(guard) {
    if (this.phase !== "plain" && this.phase !== "claimed") return [];
    switch (guard.kind) {
      case "span": {
        const span = guard.span;
        if (!this.casOk(span) || span.start === span.end) return [];
        this.pushTxn();
        this.typingRun = undefined;
        this.reconcile({ start: span.start, end: span.end, insertedLength: 0 });
        this.adopt(this.draft.slice(0, span.start) + this.draft.slice(span.end));
        this.watchClaim();
        this.paste = undefined;
        return [];
      }
      case "bare-token": {
        if (guard.token === "" || this.draft.trim() !== guard.token) return [];
        this.pushTxn();
        this.typingRun = undefined;
        this.occurrences = [];
        this.adopt("");
        this.watchClaim();
        this.paste = undefined;
        return [];
      }
      default: return unreachable(guard);
    }
  }

  /** 失效位：非事务（不动草稿/修订/撤销日志；原文 :344-354）。 */
  onSetInvalid(invalidIds) {
    const ids = new Set(invalidIds);
    if (!this.occurrences.some((o) => (o.invalid === true) !== ids.has(o.occurrenceId))) return [];
    this.occurrences = this.occurrences.map((o) => {
      const invalid = ids.has(o.occurrenceId);
      if ((o.invalid === true) === invalid) return o;
      const rest = Object.assign({}, o);
      delete rest.invalid;
      return invalid ? Object.assign(rest, { invalid: true }) : rest;
    });
    return [];
  }

  // ---- 撤销 / 重做 ----

  onUndo() {
    const entry = this.log.pop();
    if (entry === undefined) return [];
    this.redoStack.push({ draftBefore: this.draft, occurrencesBefore: this.occurrences });
    this.occurrences = entry.occurrencesBefore;
    this.adopt(entry.draftBefore);
    this.watchClaim();
    this.typingRun = undefined;
    this.paste = undefined;
    return [];
  }

  onRedo() {
    const entry = this.redoStack.pop();
    if (entry === undefined) return [];
    // 手工压栈：pushTxn 会切断正在走的 redo 链（原文 :373）。
    this.log.push({ draftBefore: this.draft, occurrencesBefore: this.occurrences });
    if (this.log.length > LOG_LIMIT) this.log.shift();
    this.occurrences = entry.occurrencesBefore;
    this.adopt(entry.draftBefore);
    this.watchClaim();
    this.typingRun = undefined;
    this.paste = undefined;
    return [];
  }

  // ---- 粘贴面 ----

  /** 原文 :392-429 */
  onPasteBegin(rawText, selection, components = [], generation = 0) {
    const { start, end } = selection;
    if (start < 0 || start > end || end > this.draft.length) return [];
    const text = rawText.split(PLACEHOLDER).join("");
    this.pushTxn(selection);
    this.typingRun = undefined;
    const sorted = components.slice().sort((a, b) => a.start - b.start);
    const minted = [];
    let inserted = "";
    let cursor = 0;
    for (const c of sorted) {
      inserted += text.slice(cursor, c.start);
      minted.push(this.mint(c.reference, start + inserted.length));
      inserted += PLACEHOLDER;
      cursor = c.end;
    }
    inserted += text.slice(cursor);
    this.reconcile({ start, end, insertedLength: inserted.length });
    this.withMinted(minted);
    this.adopt(this.draft.slice(0, start) + inserted + this.draft.slice(end));
    this.watchClaim();
    if (this.phase === "plain" || this.phase === "claimed") {
      this.pasteSeq += 1;
      this.paste = {
        attemptId: this.pasteSeq,
        insertedRange: { start, end: start + inserted.length },
        generation,
      };
    } else {
      this.paste = undefined;
    }
    return [];
  }

  /** 原文 :436-447 */
  onPasteUpgrade(attemptId, span, reference) {
    const attempt = this.paste;
    if (attempt === undefined || attempt.attemptId !== attemptId) return [];
    if (this.phase !== "plain" && this.phase !== "claimed") return [];
    if (!this.casOk(span) || span.start === span.end) return [];
    const insertedLength = this.replaceSpanWithChip(reference, span);
    this.paste = {
      attemptId: attempt.attemptId,
      insertedRange: {
        start: attempt.insertedRange.start,
        end: attempt.insertedRange.end + insertedLength - (span.end - span.start),
      },
      generation: attempt.generation,
    };
    return [];
  }

  // ---- 提交面 ----

  /** 铸下一个 SubmitAttempt 并占用 in-flight 槽（原文 :452-458）。 */
  beginAttempt(mode) {
    const controller = new AbortController();
    this.seq += 1;
    const attempt = { seq: this.seq, signal: controller.signal, draftSnapshot: this.draft, mode };
    this.inflight = { attempt, controller };
    return attempt;
  }

  /**
   * 原文 :460-477
   * ★ 忙时丢弃本次 Enter：adjudicating / submitting 直接返回 []（不吞草稿、不报错）。
   *   注意「运行中」不是这两个相位——运行中按 plain，Enter 照常走 default-sink，
   *   由 shell 按 submission-policy 决定排队还是注入（任务书 §一.2）。
   */
  onEnter(mode) {
    if (this.phase === "adjudicating" || this.phase === "submitting") return [];
    if (this.phase === "claimed" && this.claim !== undefined) {
      const attempt = this.beginAttempt(mode);
      this.phase = "submitting";
      this.paste = undefined;
      return [{ type: "begin-submit", attempt, claim: this.claim, args: argsAfter(this.draft, this.claim.token) }];
    }
    const trimmed = this.draft.trim();
    if (trimmed === "") return [];
    this.paste = undefined;
    if (trimmed.startsWith("/")) {
      const attempt = this.beginAttempt(mode);
      this.phase = "adjudicating";
      return [{ type: "adjudicate", attempt, draft: this.draft }];
    }
    return [{ type: "default-sink", draft: this.draft, mode }];
  }

  /** 原文 :479-499 */
  onAdjudicated(attempt, outcome) {
    const flight = this.inflight;
    if (this.phase !== "adjudicating" || flight === undefined || flight.attempt.seq !== attempt.seq) return [];
    if (outcome !== undefined && outcome !== "handled" && "claim" in outcome) {
      this.claim = outcome.claim;
      this.phase = "submitting";
      return [{
        type: "begin-submit",
        attempt,
        claim: outcome.claim,
        args: argsAfter(attempt.draftSnapshot, outcome.claim.token),
      }];
    }
    this.inflight = undefined;
    this.phase = "plain";
    return outcome === undefined
      ? [{ type: "default-sink", draft: attempt.draftSnapshot, mode: attempt.mode }]
      : [];
  }

  /** 原文 :501-507 —— 草稿保住（预热失败绝不静默降级成提示）。 */
  onAdjudicationFailed(attempt, message) {
    if (this.phase !== "adjudicating" || this.inflight?.attempt.seq !== attempt.seq) return [];
    this.inflight = undefined;
    this.phase = "plain";
    return [{ type: "notice", level: "error", text: message }];
  }

  /** 原文 :509-540 */
  onSubmitSettled(ev) {
    const flight = this.inflight;
    if (this.phase !== "submitting" || flight === undefined || flight.attempt.seq !== ev.attempt.seq) return [];
    this.inflight = undefined;
    if (ev.ok) {
      this.phase = "plain";
      this.claim = undefined;
      this.occurrences = [];
      this.adopt("");
      // 已提交内容一去不返：撤销不得把「已发送的草稿」复活（原文 :518-519）。
      this.log = [];
      this.redoStack = [];
      this.typingRun = undefined;
      this.paste = undefined;
      return ev.outcome?.text !== undefined
        ? [{ type: "notice", level: ev.outcome.kind === "error" ? "error" : "info", text: ev.outcome.text }]
        : [];
    }
    const text = ev.message ?? ev.outcome?.text ?? "command failed";
    if (this.draft === flight.attempt.draftSnapshot
      && this.claim !== undefined && this.draft.startsWith(this.claim.token)) {
      this.phase = "claimed";
      return [{ type: "notice", level: "error", text }];
    }
    this.phase = "plain";
    this.claim = undefined;
    return [{ type: "notice", level: "error", text }];
  }

  /** 普通发送被接受：按提交清理（无撤销单元；原文 :544-553）。 */
  onSendCommitted() {
    this.claim = undefined;
    this.occurrences = [];
    this.adopt("");
    this.log = [];
    this.redoStack = [];
    this.typingRun = undefined;
    this.paste = undefined;
    return [];
  }

  /** 原文 :555-565 */
  onRelease() {
    if (this.inflight !== undefined) {
      this.inflight.controller.abort();
      this.inflight = undefined;
    }
    this.phase = "plain";
    this.claim = undefined;
    this.typingRun = undefined;
    this.paste = undefined;
    return [];
  }
}
