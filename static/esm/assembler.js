/* esm/assembler.js —— 会话渲染装配器（纯状态机 · 零 DOM · 零依赖）
 *
 *   裁定落点（任务书《会话渲染单源化》C1/C2）：
 *     · C1 单源单通道：timeline（落盘事件流）是唯一渲染数据源；
 *     · C2 位置由数据自带：节点位置＝(分组键, 到达序)，渲染只读快照，
 *       绝不数 DOM、不找锚点、不做归位推算。
 *
 *   ── 分组键的实测依据（施工前核验，与任务书字面写法的一处必要偏差）──
 *   任务书 §三件1 写「snapshot 按 round 分组」。核验 48 张卡的 events jsonl
 *   （works 下各 work 的 events 目录，全量逐条读）后确认：
 *   **顶层 round 不是对话轮次**：
 *     · round 由 event_stream 的回合锚 turn 决定，run_start 置 0、每次
 *       round_start 改成模型轮次计数（event_stream.py:316-322）——
 *       同一个 run 内它会 0,1,2,3 递增，**下一个 run 又回 0**；
 *     · assistant_text 走 session_facade.emit_message_event 的独立落点，
 *       round 由调用方另给（实测同卡里出现 round=0 与 round=1 两种取值）；
 *     · 48 张卡里 4 张 round 非单调；work_001/card_003 全卡 round=0（952 条）。
 *   真正的对话轮次锚是 **run_id（= pair_id 回合对）**：一条 user_message 与它
 *   的过程事件、assistant_text 共用同一个 run_id（实测 work_009/card_001：
 *   seq19~36 全为 248f63b4＝一轮）。故分组键：
 *     run_id 非空 → 「run:」+ run_id（生产卡 43/48 张）；
 *     run_id 为空 → 「r0:」+ round（迁移老卡 5 张，其 round 确为轮次序号）。
 *   这仍是「位置由数据自带」，且是全量落盘数据里唯一可证的轮次锚；
 *   按字面的 round 分组会把每一轮的 round=0 前缀（user_message/run_start/
 *   context/…）全部并进同一组——正是本任务要消灭的跨轮混排。
 *   偏差与证据见《会话渲染单源化_施工回报_20261015.md》。
 *
 *   ── 两套 seq 空间的隔离 ──
 *   落盘 timeline 的 seq 来自 events 文件尾取号，实时 SSE 的 seq 来自内存
 *   journal 计数器（stream.js 旧注释已点明二者不相等）。两路若混进同一个
 *   去重集合会互相吞帧，故每条记录带 ns（"disk"/"live"），去重与排序都按
 *   (ns, seq)／到达序进行，互不串扰。
 *
 *   ── 排序键是「到达序」不是 seq 数值 ──
 *   装配器只按到达顺序 applyRecord，而到达顺序恒等于真实时序（落盘文件是
 *   append-only 的时间序，SSE 帧按到达序推）。故每个节点记一个内部单调
 *   ord 作排序键：全量重建时它＝文件序，实时时它＝发生序，混流时它＝
 *   先 disk 后 live 的真实先后。
 *
 *   导出：
 *     REGISTRY           类型注册表（纯数据：加事件类型＝加一行）
 *     createAssembler()  { applyRecord, rebuild, snapshot }
 */

/* ── ① 类型注册表 ─────────────────────────────────────────────────
   type → { kind, merge? }
   kind ∈ user / assistant / cardmsg / tool / chain / seats / sys / injection
   kind="reasoning" 是特例：带 model → 并进 seats 组；不带 → 并进同组
   assistant 的思考区。注册表里没有的类型一律兜底 sys（C5：不丢不炸）。 */
export const REGISTRY = {
  user_message:          { kind: "user" },
  assistant_text:        { kind: "assistant" },
  msg_ask: { kind: "cardmsg" }, msg_answer: { kind: "cardmsg" }, msg_notify: { kind: "cardmsg" },

  tool_call:     { kind: "tool", merge: "call" },   // 同 key 合体
  tool_result:   { kind: "tool", merge: "result" },

  judgment: { kind: "chain" }, plan_created: { kind: "chain" },
  step_start: { kind: "chain" }, step_advanced: { kind: "chain" },

  seats_enter: { kind: "seats" }, seat_start: { kind: "seats", merge: "member" },
  seat_end: { kind: "seats", merge: "member" },

  synthesize: { kind: "sys" }, deliver: { kind: "sys" }, proposal: { kind: "sys" },
  done: { kind: "sys" }, error: { kind: "sys" }, rescue: { kind: "sys" },
  card_op: { kind: "sys" }, context: { kind: "sys" }, round_start: { kind: "sys" },
  round_end: { kind: "sys" }, run_start: { kind: "sys" },

  context_preamble: { kind: "injection" }, context_injection: { kind: "injection" },
  judgment_protocol: { kind: "injection" }, context_seat_identity: { kind: "injection" },

  // reasoning 特例：带 model → seats 席卡；不带 → merge 进同组 assistant 思考区
  reasoning: { kind: "reasoning" },

  // ↓ 以下三类不在任务书给定的注册表清单里，但**真实落盘数据存在**
  //   （实测全量 type 统计：card_message 见于 cards 互发、triage_verdict
  //   见于退役前的旧 journal、ping 是 SSE 保活）。按注册表自带的扩展机制
  //   「加事件类型＝加一行」补齐，否则它们会掉进 C5 兜底行、显示成
  //   「未识别事件」，白丢可读的旧渲染分支。
  card_message: { kind: "sys" },      // 旧 renderEvent 有专属 case（卡间消息）
  triage_verdict: { kind: "sys" },    // 旧 renderEvent 有专属 case（已退役）
  ping: { kind: "sys" },              // SSE 保活，renderEvent 原先无 case
};

/** revoke 是控制位记录：不渲染成节点，只驱动软删。 */
const CONTROL_TYPE = "revoke";

function kindOf(type) {
  const hit = REGISTRY[type];
  return hit ? hit.kind : "sys";
}

function toInt(v, dflt) {
  const n = typeof v === "number" ? v : parseInt(v, 10);
  return Number.isFinite(n) ? n : (dflt === undefined ? 0 : dflt);
}

/** 记录顶层 model（落盘形态）；data.model 是实时帧形态，两处都认。 */
function modelOf(rec, data) {
  return String((rec && rec.model) || (data && data.model) || "");
}

/** 分组键：run_id 优先（对话轮次），缺失回落 round（迁移老卡轮次序号）。 */
export function groupKeyOf(rec) {
  const runId = String((rec && rec.run_id) || "");
  if (runId) return "run:" + runId;
  return "r0:" + toInt(rec && rec.round, 0);
}

/* ── ② 装配器（纯函数式状态机）────────────────────────────────── */
export function createAssembler() {
  /** 分组键 → 组。组内 order 即到达序。 */
  const groups = new Map();
  /** "ns|seq" 去重表：同一条记录二次入装配不重不炸。 */
  const seen = new Set();
  /** 全局到达计数（排序键）。 */
  let ord = 0;

  function newGroup(rec) {
    const key = groupKeyOf(rec);
    let g = groups.get(key);
    if (!g) {
      g = {
        key,
        runId: String((rec && rec.run_id) || ""),
        round: toInt(rec && rec.round, 0),
        roundMax: toInt(rec && rec.round, 0),
        nodes: [],
        bySeq: new Map(),       // 组内 seq → 节点（tool 合体配对用）
        lastOpenTool: null,     // 组内最近一条未闭合 tool 节点
        seats: null,            // 组内 seats 节点（懒建）
        pendingReasoning: "",   // 该轮尚无 assistant 时暂存的思考（待吸收）
        pendingReasoningOrd: 0,
        terminal: false,        // 该组已见 done/error（轮界已收口）
      };
      groups.set(key, g);
    }
    return g;
  }

  function pushNode(g, node) {
    node.ord = ++ord;
    node.groupKey = g.key;
    g.nodes.push(node);
    g.bySeq.set(node.seq, node);
    const r = toInt(node.round, 0);
    if (r > g.roundMax) g.roundMax = r;
    return node;
  }

  /** 组内 seats 节点：seats_enter 开组；seat_start / 带 model 的 reasoning 懒建。 */
  function seatsNodeOf(g, rec) {
    if (!g.seats) {
      g.seats = pushNode(g, {
        seq: toInt(rec.seq, 0), ns: rec.ns, type: rec.type, kind: "seats",
        round: rec.round, data: rec.data || {}, model: modelOf(rec, rec.data),
        members: {}, entered: false,
      });
    }
    return g.seats;
  }

  function memberOf(seats, model) {
    const key = String(model || "");
    if (!key) return null;
    if (!seats.members[key]) seats.members[key] = { model: key, thinking: "", start: null, end: null };
    return seats.members[key];
  }

  /** 组内最近一条 assistant 节点（思考归属目标；无则挂 pending）。 */
  function lastAssistantOf(g) {
    for (let i = g.nodes.length - 1; i >= 0; i--) {
      if (g.nodes[i].kind === "assistant") return g.nodes[i];
    }
    return null;
  }

  function appendReasoning(g, text) {
    if (!text) return;
    const target = lastAssistantOf(g);
    if (target) {
      target.reasoning = (target.reasoning || "") + text;
    } else {
      g.pendingReasoning += text;
      if (!g.pendingReasoningOrd) g.pendingReasoningOrd = ord + 1;
    }
  }

  /** tool_result 的配对目标：tool_call_id 精确优先，其次同组最近未闭合调用行。 */
  function toolTargetOf(g, d) {
    const id = d && (d.tool_call_id || d.call_id);
    if (id) {
      for (const n of g.nodes) {
        if (n.kind === "tool" && n.call && (n.call.tool_call_id || n.call.call_id) === id) return n;
      }
    }
    for (let i = g.nodes.length - 1; i >= 0; i--) {
      const n = g.nodes[i];
      if (n.kind === "tool" && !n.closed) return n;
    }
    return null;
  }

  /** 软删：revoke{from_seq:F} 撤下**此刻已装配**的 seq>=F 节点。
      与后端 event_log.timeline 的位置语义逐字一致（只撤 revoke 之前
      累积的段；revoke 之后的新 run 不受影响）。 */
  function purgeFrom(g, fromSeq) {
    g.nodes = g.nodes.filter((n) => toInt(n.seq, 0) < fromSeq);
    g.bySeq = new Map(g.nodes.map((n) => [n.seq, n]));
    g.lastOpenTool = null;
    g.seats = null;
    g.terminal = false;
    for (const n of g.nodes) {
      if (n.kind === "tool" && !n.closed) g.lastOpenTool = n;
      if (n.kind === "seats") g.seats = n;
      if (n.type === "done" || n.type === "error") g.terminal = true;
    }
    g.pendingReasoning = "";
  }

  function applyRecord(rec) {
    if (!rec || typeof rec !== "object") return null;
    const type = String(rec.type || "");
    if (!type) return null;

    const ns = String(rec.ns || "disk");
    const seq = toInt(rec.seq, 0);
    const dedupKey = ns + "|" + type + "|" + seq;
    if (seen.has(dedupKey)) return null;
    seen.add(dedupKey);

    // data 非 dict 时按后端同语义装箱（event_stream._persist_log：
    // `payload = data if isinstance(data, dict) else {"value": data}`）——
    // 绝不静默丢载荷，也绝不因畸形帧炸掉整轮装配。
    let data = rec.data;
    if (data === undefined || data === null) data = {};
    else if (typeof data !== "object") data = { value: data };
    const g = newGroup(rec);

    if (type === CONTROL_TYPE) {            // 控制位：软删，不成节点
      const f = toInt(data.from_seq, -1);
      if (f >= 0) purgeFrom(g, f);
      return null;
    }

    const kind = kindOf(type);
    const base = {
      seq, ns, type, kind, round: toInt(rec.round, 0), data, model: modelOf(rec, data),
    };

    if (kind === "user" || kind === "assistant" || kind === "cardmsg") {
      const n = pushNode(g, base);
      if (kind === "assistant") {
        n.reasoning = "";
        if (g.pendingReasoning) {           // pending 思考在此归位（吸收）
          n.reasoning = g.pendingReasoning;
          g.pendingReasoning = "";
          g.pendingReasoningOrd = 0;
        }
      }
      return n;
    }

    if (kind === "tool") {
      const merge = REGISTRY[type].merge;
      if (merge === "call") {
        const n = pushNode(g, Object.assign(base, { call: data, result: null, closed: false }));
        g.lastOpenTool = n;
        return n;
      }
      // result：合体到配对的 call 节点（保留 call 的 seq＝位置），无配对则独立成节点
      const target = toolTargetOf(g, data);
      if (target) {
        target.result = data;
        target.closed = true;
        target.type = "tool_result";        // 渲染器按两态终态出
        target.data = data;
        if (target === g.lastOpenTool) g.lastOpenTool = null;
        return target;
      }
      const n = pushNode(g, Object.assign(base, { call: null, result: data, closed: true }));
      return n;
    }

    if (kind === "seats") {
      const merge = REGISTRY[type].merge;
      const node = seatsNodeOf(g, rec);
      if (type === "seats_enter") {
        node.entered = true;
        node.enter = data;
        node.data = data;
        return node;
      }
      const model = String(data.model || "");
      if (merge === "member" && model) {
        const m = memberOf(node, model);
        if (type === "seat_start") m.start = data;
        else if (type === "seat_end") m.end = data;
        return node;
      }
      return node;
    }

    if (kind === "reasoning") {
      const model = modelOf(rec, data);
      const text = String(data.text || "");
      if (model) {                            // 三席思考 → 席卡
        const node = seatsNodeOf(g, rec);
        const m = memberOf(node, model);
        if (m) m.thinking += text;
        return node;
      }
      appendReasoning(g, text);               // 主模型思考 → 同组 assistant
      return null;
    }

    const node = pushNode(g, base);          // chain / sys / injection
    if (type === "done" || type === "error") g.terminal = true;
    return node;
  }

  function rebuild(items) {
    groups.clear();
    seen.clear();
    ord = 0;
    if (Array.isArray(items)) {
      for (const rec of items) applyRecord(rec);
    }
    return snapshot();
  }

  function snapshot() {
    const rounds = [];
    const lastKey = groups.size ? Array.from(groups.keys()).pop() : "";
    for (const [key, g] of groups) {
      if (!g.nodes.length) continue;
      const nodes = g.nodes.slice().sort((a, b) => a.ord - b.ord);
      const out = {
        key,
        round: g.round,
        roundMax: g.roundMax,
        runId: g.runId,
        nodes,
      };
      // 该轮有思考却始终没有正式回复（run 以 error 收尾、或本轮已被后续轮
      // 顶替）→ 不丢，落一条兜底 sys 节点（旧实现在此处直接丢弃思考文本）。
      // **只在确定不会再有 assistant 时兜底**：轮已收口（done/error）或本组
      // 之后又来了新轮。进行中的 pending 不兜底，否则实时渲染会先闪一条
      // 「无归属思考」，等 assistant_text 到达又被吸收掉（闪一下又消失）。
      const finished = g.terminal || key !== lastKey;
      if (g.pendingReasoning && finished) {
        const tail = nodes[nodes.length - 1];
        out.nodes.push({
          ord: g.pendingReasoningOrd || (tail.ord + 0.5),
          seq: tail.seq,
          ns: tail.ns,
          type: "reasoning",
          kind: "sys",
          round: g.round,
          data: { text: g.pendingReasoning, orphan: true },
          model: "",
          groupKey: g.key,
        });
      }
      rounds.push(out);
    }
    rounds.sort((a, b) => (a.nodes[0].ord) - (b.nodes[0].ord));
    return { rounds };
  }

  return { applyRecord, rebuild, snapshot };
}
