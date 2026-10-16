/* esm/assembler.js —— 会话渲染装配器（纯状态机 · 零 DOM · 零依赖）
 *
 *   裁定落点（任务书《会话渲染单源化》C1/C2 ＋《对话流照抄 dsh》T1/T2）：
 *     · C1 单源单通道：timeline（落盘事件流）是唯一渲染数据源；
 *     · C2 位置由数据自带：节点位置＝(分组键, 到达序)，渲染只读快照，
 *       绝不数 DOM、不找锚点、不做归位推算；
 *     · dsh A1/A2（增量装配）：每个节点带**稳定 key** 与**内容版本 rev**，
 *       渲染层据此复用 DOM／原地更新，不整列重算。快照对象在结构未变时
 *       返回**同一引用**（sameReferences 语义）。
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
 *     run_id 为空 → 「r0:」+ round（迁移老卡 5 张，其 round 确为轮次序号）；
 *     ns="local"  → 「local:」+ local_key（乐观上屏的临时轮，见下）。
 *   这仍是「位置由数据自带」，且是全量落盘数据里唯一可证的轮次锚；
 *   按字面的 round 分组会把每一轮的 round=0 前缀（user_message/run_start/
 *   context/…）全部并进同一组——正是本任务要消灭的跨轮混排。
 *   偏差与证据见《会话渲染单源化_施工回报_20261015.md》。
 *
 *   ── 两套 seq 空间的隔离 ──
 *   落盘 timeline 的 seq 来自 events 文件尾取号，实时 SSE 的 seq 来自内存
 *   journal 计数器（stream.js 旧注释已点明二者不相等）。两路若混进同一个
 *   去重集合会互相吞帧，故每条记录带 ns（"disk"/"live"/"local"），去重与排序
 *   都按 (ns, seq)／到达序进行，互不串扰。
 *
 *   ── 排序键是「到达序」不是 seq 数值 ──
 *   装配器只按到达顺序 applyRecord，而到达顺序恒等于真实时序（落盘文件是
 *   append-only 的时间序，SSE 帧按到达序推）。故每个节点记一个内部单调
 *   ord 作排序键：全量重建时它＝文件序，实时时它＝发生序，混流时它＝
 *  先 disk 后 live 的真实先后。
 *
 *   ── 乐观上屏（T1）与「本地轮认领」──
 *   点发送的瞬间，前端以 ns="local" 记一条 user_message，自成一组
 *   （key="local:<local_key>"）。随后该轮的第一条**带 run_id 的真实记录**
 *   （SSE 的 run_start，或 timeline 回流的 user_message）到达时，装配器把
 *   本地组**改名**为 "run:<run_id>"（认领），于是该轮后续过程事件天然落进
 *   同一组，不产生第二个轮容器、不搬任何既有 DOM。
 *   真实 user_message 随后到达时，若组内已有内容相同的 pending 节点，则
 *   **原地升级**（补 seq/data、清 pending），不新增节点、不出双气泡。
 *
 *   导出：
 *     REGISTRY              类型注册表（纯数据：加事件类型＝加一行）
 *     groupKeyOf(rec)       分组键（run_id 优先 → round 回落 → local_key）
 *     createAssembler()     { applyRecord, rebuild, snapshot, dropLocal }
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
  // [更新现状按钮改造 2026-10-16] 「更新现状」专用回合的轮首任务注入（同注入族）。
  status_update_task: { kind: "injection" },

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

/** 分组键：run_id 优先（对话轮次），缺失回落 round（迁移老卡轮次序号）；
    乐观上屏的本地记录（ns="local"）用 local_key 单独成组。 */
export function groupKeyOf(rec) {
  if (rec && String(rec.ns || "") === "local") {
    return "local:" + String((rec && rec.local_key) || "0");
  }
  const runId = String((rec && rec.run_id) || "");
  if (runId) return "run:" + runId;
  return "r0:" + toInt(rec && rec.round, 0);
}

/* ── ② 装配器（纯函数式状态机）────────────────────────────────── */
export function createAssembler() {
  /** 分组键 → 组。 */
  const groups = new Map();
  /** 组对象数组（创建序＝首节点到达序，快照轮序即此序）。 */
  const groupList = [];
  /** "ns|type|id" 去重表：同一条记录二次入装配不重不炸。 */
  const seen = new Set();
  /** 全局到达计数（排序键）。 */
  let ord = 0;
  /** 结构版本：仅在「增删节点／增删组／改键」时自增——快照据此判引用可复用。 */
  let structRev = 0;
  /** 待认领的本地轮（乐观上屏创建的组键）。 */
  let pendingLocalKey = null;
  /** 快照缓存（sameReferences：结构没变就返回同一引用）。 */
  let snapCache = null;
  let snapCacheRev = -1;

  function newGroup(rec) {
    const key = groupKeyOf(rec);
    let g = groups.get(key);
    if (!g) {
      // 新轮出现 ⇒ 此前所有轮已收口（不再有 assistant 到达），可落兜底思考。
      for (const other of groupList) if (other.key !== key) finalizeGroup(other);
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
        finalized: false,       // 兜底思考已落（幂等）
        snap: null,             // 快照包装对象（引用稳定）
      };
      groups.set(key, g);
      groupList.push(g);
      structRev++;
    }
    return g;
  }

  function pushNode(g, node) {
    node.ord = ++ord;
    node.rev = 1;
    node.groupKey = g.key;
    g.nodes.push(node);
    g.bySeq.set(node.seq, node);
    const r = toInt(node.round, 0);
    if (r > g.roundMax) g.roundMax = r;
    structRev++;
    return node;
  }

  /** 节点稳定键：同一节点跨多次 apply 恒等（渲染层据此复用 DOM）。
      组内 (ns, seq, kind) 唯一——seq 由后端单卡单调取号，tool_result 合体
      不新增 seq，故不会撞键。 */
  function keyOf(ns, seq, kind) {
    return String(ns) + ":" + toInt(seq, 0) + ":" + String(kind);
  }

  /** 组内 seats 节点：seats_enter 开组；seat_start / 带 model 的 reasoning 懒建。 */
  function seatsNodeOf(g, rec) {
    if (!g.seats) {
      const seq = toInt(rec.seq, 0);
      g.seats = pushNode(g, {
        seq, ns: rec.ns, type: rec.type, kind: "seats",
        round: rec.round, data: rec.data || {}, model: modelOf(rec, rec.data),
        members: {}, entered: false,
        key: keyOf(rec.ns || "disk", seq, "seats"),
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
      target.rev++;
    } else {
      g.pendingReasoning += text;
      if (!g.pendingReasoningOrd) g.pendingReasoningOrd = ord + 1;
    }
  }

  /** 该轮收口：思考若始终无人认领，落一条兜底 sys 节点（不丢文本）。
      **只在确定不会再有 assistant 时**落：轮已收口（done/error）或本组之后
      又来了新轮。进行中的 pending 不落，否则实时渲染会先闪一条「无归属
      思考」，等 assistant_text 到达又被吸收掉（闪一下又消失）。 */
  function finalizeGroup(g) {
    if (g.finalized) return;
    g.finalized = true;
    structRev++;                 // 收口位变化 ⇒ 快照需重建（折叠开关据此出现）
    if (g.pendingReasoning && !g.nodes.some((n) => n.kind === "assistant")) {
      const tail = g.nodes[g.nodes.length - 1];
      const seq = tail ? tail.seq : 0;
      const ns = (tail && tail.ns) || "disk";
      const node = {
        seq, ns, type: "reasoning", kind: "sys", round: g.round,
        data: { text: g.pendingReasoning, orphan: true }, model: "",
        key: String(ns) + ":orphan:" + toInt(seq, 0),
        rev: 1,
        ord: g.pendingReasoningOrd || ((tail ? tail.ord : 0) + 0.5),
        groupKey: g.key,
      };
      g.nodes.push(node);
      g.nodes.sort((a, b) => a.ord - b.ord);
      structRev++;
    }
    g.pendingReasoning = "";
  }

  /** 本地轮认领：把 "local:<k>" 组改名成 "run:<run_id>"。 */
  function adoptLocalGroup(runId) {
    const oldKey = pendingLocalKey;
    pendingLocalKey = null;
    if (!oldKey) return;
    const g = groups.get(oldKey);
    if (!g) return;
    groups.delete(oldKey);
    g.key = "run:" + runId;
    g.runId = runId;
    for (const n of g.nodes) n.groupKey = g.key;
    groups.set(g.key, g);
    structRev++;
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
    const before = g.nodes.length;
    const wasFinalized = g.finalized;
    g.nodes = g.nodes.filter((n) => toInt(n.seq, 0) < fromSeq);
    g.bySeq = new Map(g.nodes.map((n) => [n.seq, n]));
    g.lastOpenTool = null;
    g.seats = null;
    g.terminal = false;
    g.finalized = false;
    for (const n of g.nodes) {
      if (n.kind === "tool" && !n.closed) g.lastOpenTool = n;
      if (n.kind === "seats") g.seats = n;
      if (n.type === "done" || n.type === "error") g.terminal = true;
    }
    g.pendingReasoning = "";
    g.pendingReasoningOrd = 0;
    if (g.nodes.length !== before || wasFinalized) structRev++;
  }

  function applyRecord(rec) {
    if (!rec || typeof rec !== "object") return null;
    const type = String(rec.type || "");
    if (!type) return null;

    const ns = String(rec.ns || "disk");
    const seq = toInt(rec.seq, 0);
    const isLocal = ns === "local";
    // 去重键：本地记录无落盘 seq（恒 0），用 local_key 代替，避免互相吞帧。
    const dedupId = isLocal ? String(rec.local_key || "0") : seq;
    const dedupKey = ns + "|" + type + "|" + dedupId;
    if (seen.has(dedupKey)) return null;
    seen.add(dedupKey);

    // data 非 dict 时按后端同语义装箱（event_stream._persist_log：
    // `payload = data if isinstance(data, dict) else {"value": data}`）——
    // 绝不静默丢载荷，也绝不因畸形帧炸掉整轮装配。
    let data = rec.data;
    if (data === undefined || data === null) data = {};
    else if (typeof data !== "object") data = { value: data };

    // 本地轮认领：本轮第一条带 run_id 的真实记录到达 ⇒ 本地组改名入正式轮。
    const runId = String(rec.run_id || "");
    if (!isLocal && runId && pendingLocalKey) adoptLocalGroup(runId);

    const g = newGroup(rec);

    if (type === CONTROL_TYPE) {            // 控制位：软删，不成节点
      const f = toInt(data.from_seq, -1);
      if (f >= 0) purgeFrom(g, f);
      return null;
    }

    const kind = kindOf(type);
    const base = {
      seq, ns, type, kind, round: toInt(rec.round, 0), data, model: modelOf(rec, data),
      key: keyOf(ns, seq, kind),
    };

    if (kind === "user" || kind === "assistant" || kind === "cardmsg") {
      if (kind === "user") {
        // 乐观上屏的 pending 节点：真实 user_message 到达 ⇒ 原地升级（不出双气泡）。
        const pend = g.nodes.find((n) => n.kind === "user" && n.pending);
        if (pend && sameText(pend.data && pend.data.content, data && data.content)) {
          pend.pending = false;
          pend.seq = seq;
          pend.ns = ns;
          pend.data = data;
          pend.type = type;
          pend.rev++;
          return pend;
        }
      }
      const n = pushNode(g, base);
      if (kind === "user" && isLocal) {
        n.pending = true;
        n.localKey = rec.local_key;
        pendingLocalKey = g.key;
      }
      if (kind === "assistant") {
        n.reasoning = "";
        if (g.pendingReasoning) {           // pending 思考在此归位（吸收）
          n.reasoning = g.pendingReasoning;
          n.rev++;
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
        target.rev++;
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
        node.rev++;
        return node;
      }
      const model = String(data.model || "");
      if (merge === "member" && model) {
        const m = memberOf(node, model);
        if (type === "seat_start") m.start = data;
        else if (type === "seat_end") m.end = data;
        node.rev++;
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
        if (m) { m.thinking += text; node.rev++; }
        return node;
      }
      appendReasoning(g, text);               // 主模型思考 → 同组 assistant
      return null;
    }

    const node = pushNode(g, base);          // chain / sys / injection
    if (type === "done" || type === "error") {
      g.terminal = true;
      finalizeGroup(g);                      // 轮界收口 ⇒ 兜底思考落位
    }
    return node;
  }

  function sameText(a, b) {
    return String(a == null ? "" : a).trim() === String(b == null ? "" : b).trim();
  }

  function rebuild(items) {
    groups.clear();
    groupList.length = 0;
    seen.clear();
    ord = 0;
    structRev++;
    pendingLocalKey = null;
    snapCache = null;
    snapCacheRev = -1;
    if (Array.isArray(items)) {
      for (const rec of items) applyRecord(rec);
    }
    return snapshot();
  }

  /** 丢弃全部本地轮（乐观上屏的临时气泡）——发送失败时回退用。 */
  function dropLocal() {
    let hit = false;
    for (let i = groupList.length - 1; i >= 0; i--) {
      const g = groupList[i];
      if (g.key.indexOf("local:") !== 0) continue;
      groups.delete(g.key);
      groupList.splice(i, 1);
      hit = true;
    }
    if (pendingLocalKey) { pendingLocalKey = null; hit = true; }
    if (hit) structRev++;
    return hit;
  }

  /** 快照：结构未变 ⇒ 返回**同一引用**（渲染层据此零重排）。
      节点对象本身是活引用——内容变化（rev 自增）透过节点可见，无需换数组。 */
  function snapshot() {
    if (snapCache && snapCacheRev === structRev) return snapCache;
    const rounds = [];
    for (const g of groupList) {
      if (!g.nodes.length) continue;
      if (!g.snap) {
        g.snap = { key: g.key, round: g.round, roundMax: g.roundMax, runId: g.runId,
                   finalized: g.finalized, nodes: g.nodes };
      } else {
        g.snap.key = g.key;
        g.snap.round = g.round;
        g.snap.roundMax = g.roundMax;
        g.snap.runId = g.runId;
        g.snap.finalized = g.finalized;
      }
      rounds.push(g.snap);
    }
    snapCache = { rounds };
    snapCacheRev = structRev;
    return snapCache;
  }

  return { applyRecord, rebuild, snapshot, dropLocal };
}
