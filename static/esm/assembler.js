/* esm/assembler.js —— 会话装配器（扁平节点表 · dsh chat-snapshot-builder 形态）
 *
 * [对话流照抄 dsh 完全版 v3 2026-10-17] 本文件从「按轮分组」改成 dsh 的
 * **扁平节点表**：
 *     snapshot() = { order: string[], nodes: Map<key, node>, rev }
 *   order 是可见节点键的**有序数组**（到达序），nodes 是键→节点的活引用表。
 *   位置＝数组下标，不再有「轮容器」这一层——任务书 §三 退役了轮分组三道。
 *
 * 出处：deepseek-harness/packages/client/ui-conversation/src/client/
 *       conversation-nodes/chat-snapshot-builder.ts
 *   对齐点：
 *     · orderedVisible（:135-139）＝ 按 anchorSeq 升序；本机用到达序 ord（见下）。
 *     · MutableChatNodeStore（:19-52）＝ byKey ＋ valuesCache；本机同形。
 *     · replace/apply 的**引用稳定**（:394-427）：结构未变 ⇒ order 返回同一数组、
 *       snapshot 返回同一对象；内容变化只改节点自身的 rev，不动数组。
 *     · 内容型 upsert 不触发重排（:404-419 structural 判定）；本机以 rev 表达。
 *
 * 与 dsh 的**必要差异**（数据源不同，属任务书 §0 的适配层）：
 *   · dsh 的节点由 Host 的 ConversationNode 流直接给出；本机要从 timeline 的
 *     29 类事件**装配**出节点（user/assistant/tool/seats/sys/injection…），
 *     这段装配逻辑是原 assembler 的既有语义，原样保留（只把容器从组换成扁平表）。
 *   · 排序键是「到达序」不是 seq 数值：落盘文件是 append-only 时间序、SSE 帧按
 *     到达序推，故到达序恒等于真实时序；两套 seq 空间（disk/live）不得互比。
 *
 * 导出：
 *   REGISTRY              类型注册表（加事件类型＝加一行）
 *   createAssembler()     { applyRecord, rebuild, snapshot, dropLocal, keyOfNode }
 */
/* ── ① 类型注册表（与旧版逐字一致：加事件类型＝加一行）─────────────
   kind ∈ user / assistant / cardmsg / tool / chain / seats / sys / injection
   kind="reasoning" 是特例：带 model → 并进 seats 节点；不带 → 并进最近一条
   assistant 的思考区。注册表里没有的类型一律兜底 sys（不丢不炸）。 */
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
  status_update_task: { kind: "injection" },

  reasoning: { kind: "reasoning" },
  /* [5702 · C-1 2026-10-20] 逐字流增量直接进装配器（dsh `assistant/chunk` 语义）。
     此前它们只写旁挂 live DOM、round_start 一清就没——「中间轮文字被吞」的直接原因。 */
  reasoning_chunk: { kind: "reasoning" },

  // 真实落盘数据里存在、但不在任务书清单里的三类（见旧版注释）：
  card_message: { kind: "sys" }, triage_verdict: { kind: "sys" }, ping: { kind: "sys" },
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

/** 节点稳定键：同一节点跨多次 apply 恒等（渲染层据此复用 DOM）。 */
function keyOf(ns, seq, kind) {
  return String(ns) + ":" + toInt(seq, 0) + ":" + String(kind);
}

function sameText(a, b) {
  return String(a == null ? "" : a).trim() === String(b == null ? "" : b).trim();
}

/* ── ② 装配器（纯状态机 · 零 DOM · 零依赖）────────────────────────── */
export function createAssembler() {
  /** 扁平节点表：key → node（活引用）。 */
  let byKey = new Map();
  /** 有序键数组（到达序）；结构未变时引用稳定。 */
  let order = [];
  /** "ns|type|id" 去重表：同一条记录二次入装配不重不炸。 */
  let seen = new Set();
  /** 全局到达计数（排序键）。 */
  let ord = 0;
  /** 结构版本：仅在「增删节点」时自增——快照据此判引用可复用。 */
  let structRev = 0;
  /** 待认领的本地用户节点键（乐观上屏）。 */
  let pendingLocalKey = null;
  /** 该轮尚无 assistant 时暂存的思考（待吸收）。 */
  let pendingReasoning = "";
  let pendingReasoningOrd = 0;
  let snapCache = null;
  let snapCacheRev = -1;

  function push(node) {
    node.ord = ++ord;
    node.rev = 1;
    byKey.set(node.key, node);
    order = order.concat([node.key]);
    structRev++;
    return node;
  }

  /** 取/建**本 run+round 的流式 assistant 节点**（键 `stream:{run}:{round}`）。
      [5702 · C-1] 正文与思考都走这里：dsh 的 assistant-step 以 step 为节点，
      「只有思考、没有正文」的 step 也算可见内容（assistant.ts hasVisibleContent）。
      若只在 content_chunk 建节点，思考-only 的轮会把思考并到下一轮（实测踩到）。 */
  function streamNode(runId, roundNo, seq, ns, model) {
    const skey = "stream:" + (runId || "?") + ":" + toInt(roundNo, 0);
    let node = byKey.get(skey);
    if (node) {
      node.rev++;
      if (!node.model && model) node.model = model;
      return node;
    }
    node = {
      seq, ns, type: "assistant_text", kind: "assistant",
      round: toInt(roundNo, 0), run_id: runId,
      data: { content: "", text: "" },
      model: model || "",
      key: skey, rev: 1, ord: ++ord, streaming: true,
    };
    if (pendingReasoning) {           // 落盘历史里先到的思考随节点归位（不丢）
      node.reasoning = pendingReasoning;
      pendingReasoning = "";
      pendingReasoningOrd = 0;
    }
    byKey.set(skey, node);
    order = order.concat([skey]);
    structRev++;
    return node;
  }

  /** 追加后按 ord 校正（兜底思考会插在中间）。 */
  function resort() {
    const keys = order.slice().sort((a, b) => (byKey.get(a).ord || 0) - (byKey.get(b).ord || 0));
    order = keys;
    structRev++;
  }

  /** 最近一条 assistant 节点（主模型思考的归属目标）。 */
  function lastAssistant() {
    for (let i = order.length - 1; i >= 0; i--) {
      const n = byKey.get(order[i]);
      if (n && n.kind === "assistant") return n;
    }
    return null;
  }

  /** 同 run 的 seats 节点（三席席卡；懒建）。 */
  function seatsOf(runId, rec) {
    if (runId) {
      for (let i = order.length - 1; i >= 0; i--) {
        const n = byKey.get(order[i]);
        if (n && n.kind === "seats" && String(n.run_id || "") === runId) return n;
      }
    } else {
      for (let i = order.length - 1; i >= 0; i--) {
        const n = byKey.get(order[i]);
        if (n && n.kind === "seats") return n;
      }
    }
    const seq = toInt(rec.seq, 0);
    const ns = rec.ns || "disk";
    return push({
      seq, ns, type: rec.type, kind: "seats", round: rec.round, run_id: runId,
      data: rec.data || {}, model: modelOf(rec, rec.data),
      members: {}, entered: false,
      key: keyOf(ns, seq, "seats"),
    });
  }

  function memberOf(seats, model) {
    const key = String(model || "");
    if (!key) return null;
    if (!seats.members[key]) seats.members[key] = { model: key, thinking: "", start: null, end: null };
    return seats.members[key];
  }

  function appendReasoning(text) {
    if (!text) return;
    const target = lastAssistant();
    if (target) {
      target.reasoning = (target.reasoning || "") + text;
      target.rev++;
    } else {
      pendingReasoning += text;
      if (!pendingReasoningOrd) pendingReasoningOrd = ord + 1;
    }
  }

  /** 该轮收口：思考若始终无人认领，落一条兜底节点（不丢文本）。 */
  function flushPendingReasoning() {
    if (!pendingReasoning) return;
    if (!lastAssistant()) {
      const tailKey = order[order.length - 1];
      const tail = tailKey ? byKey.get(tailKey) : null;
      const seq = tail ? tail.seq : 0;
      const ns = (tail && tail.ns) || "disk";
      const node = {
        seq, ns, type: "reasoning", kind: "sys", round: tail ? tail.round : 0, run_id: "",
        data: { text: pendingReasoning, orphan: true }, model: "",
        key: String(ns) + ":orphan:" + toInt(seq, 0),
        rev: 1,
        ord: pendingReasoningOrd || ((tail ? tail.ord : 0) + 0.5),
      };
      if (!byKey.has(node.key)) { byKey.set(node.key, node); order = order.concat([node.key]); }
      resort();
    }
    pendingReasoning = "";
    pendingReasoningOrd = 0;
  }

  /** tool_result 的配对目标：tool_call_id 精确优先，其次最近未闭合调用行。 */
  function toolTargetOf(d) {
    const id = d && (d.tool_call_id || d.call_id);
    if (id) {
      for (let i = order.length - 1; i >= 0; i--) {
        const n = byKey.get(order[i]);
        if (n && n.kind === "tool" && n.call && (n.call.tool_call_id || n.call.call_id) === id) return n;
      }
    }
    for (let i = order.length - 1; i >= 0; i--) {
      const n = byKey.get(order[i]);
      if (n && n.kind === "tool" && !n.closed) return n;
    }
    return null;
  }

  /** 软删：revoke{from_seq:F} 撤下此刻已装配的 seq>=F 节点。 */
  function purgeFrom(fromSeq) {
    const keepKeys = order.filter((k) => toInt(byKey.get(k).seq, 0) < fromSeq);
    if (keepKeys.length === order.length) return;
    const keep = new Set(keepKeys);
    for (const k of Array.from(byKey.keys())) if (!keep.has(k)) byKey.delete(k);
    order = keepKeys;
    pendingReasoning = "";
    pendingReasoningOrd = 0;
    structRev++;
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

    // data 非 dict 时按后端同语义装箱（绝不静默丢载荷）
    let data = rec.data;
    if (data === undefined || data === null) data = {};
    else if (typeof data !== "object") data = { value: data };

    // 本地轮认领：本轮第一条带 run_id 的真实记录到达 ⇒ 本地节点归入该 run。
    const runId = String(rec.run_id || "");
    if (!isLocal && runId && pendingLocalKey) {
      const local = byKey.get(pendingLocalKey);
      if (local) { local.run_id = runId; local.claimed = true; local.rev++; }
      pendingLocalKey = null;
      structRev++;
    }

    if (type === CONTROL_TYPE) {
      const f = toInt(data.from_seq, -1);
      if (f >= 0) purgeFrom(f);
      return null;
    }

    /* [5702 · C-1 2026-10-20] 正文增量 → **稳定的流式 assistant 节点**。
       键＝`stream:{run_id}:{round}`（不是 seq）：同一轮所有 chunk 累积进同一个节点；
       轮界只换键、**不清字**（旧实现 round_start 直接清空 live DOM，中间轮文字永久丢失）。
       节点对渲染层就是一个普通 assistant 节点，`data.text` 为累积正文。 */
    if (type === "content_chunk") {
      const text = String(data.text || "");
      if (!text) return null;
      const node = streamNode(runId, toInt(rec.round, 0), seq, ns, modelOf(rec, data));
      node.data.text = String(node.data.text || "") + text;
      return node;
    }

    const kind = kindOf(type);
    const base = {
      seq, ns, type, kind, round: toInt(rec.round, 0), run_id: runId,
      data, model: modelOf(rec, data), key: keyOf(ns, seq, kind),
      /* [对话界面商业化 2026-10-17] 事件时间随节点走：落盘记录带 ts（ISO），
         实时帧带 ts（秒 float，已由 frameToRecord 归一）。旧记录无 ts ⇒ 空串，
         渲染层按「有则显示」处理，不做任何猜测性补值。 */
      ts: String(rec.ts || ""),
    };

    if (kind === "user" || kind === "assistant" || kind === "cardmsg") {
      if (kind === "user") {
        // 轮界：上一轮的兜底思考先落位
        flushPendingReasoning();
        // 乐观上屏的 pending 节点：真实 user_message 到达 ⇒ 原地升级（不出双气泡）
        for (let i = order.length - 1; i >= 0; i--) {
          const n = byKey.get(order[i]);
          if (!n || n.kind !== "user" || !n.pending) continue;
          if (!sameText(n.data && n.data.content, data && data.content)) break;
          n.pending = false;
          n.seq = seq;
          n.ns = ns;
          n.data = data;
          n.type = type;
          n.run_id = runId || n.run_id;
          if (rec.ts) n.ts = String(rec.ts);
          n.rev++;
          return n;
        }
      }
      /* [5702 · C-1] 落盘 assistant_text 到达：同一 run+round 的流式节点**就地定稿**，
         不另起一条（否则同一轮会出现「流式一份 + 落盘一份」双份文本）。
         `data.content` 优先于 `data.text` 渲染；text 保留作兜底（content 为空时）。 */
      if (kind === "assistant" && !isLocal) {
        const skey = "stream:" + (runId || "?") + ":" + toInt(rec.round, 0);
        const sn = byKey.get(skey);
        if (sn && sn.streaming) {
          sn.streaming = false;
          sn.seq = seq;
          sn.ns = ns;
          sn.type = type;
          sn.data = Object.assign({}, data, { text: (sn.data && sn.data.text) || "" });
          sn.model = modelOf(rec, data) || sn.model || "";
          if (rec.ts) sn.ts = String(rec.ts);
          sn.rev++;
          return sn;
        }
      }
      const node = push(Object.assign({}, base));
      if (kind === "user" && isLocal) {
        node.pending = true;
        node.localKey = rec.local_key;
        pendingLocalKey = node.key;
      }
      if (kind === "assistant") {
        node.reasoning = "";
        if (pendingReasoning) {           // pending 思考在此归位（吸收）
          node.reasoning = pendingReasoning;
          node.rev++;
          pendingReasoning = "";
          pendingReasoningOrd = 0;
        }
      }
      return node;
    }

    if (kind === "tool") {
      const merge = REGISTRY[type].merge;
      if (merge === "call") {
        return push(Object.assign({}, base, { call: data, result: null, closed: false }));
      }
      const target = toolTargetOf(data);
      if (target) {
        target.result = data;
        target.closed = true;
        target.type = "tool_result";
        target.data = data;
        target.rev++;
        return target;
      }
      return push(Object.assign({}, base, { call: null, result: data, closed: true }));
    }

    if (kind === "seats") {
      const merge = REGISTRY[type].merge;
      const node = seatsOf(runId, rec);
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
      if (model) {                              // 三席思考 → 席卡
        const node = seatsOf(runId, rec);
        const m = memberOf(node, model);
        if (m) { m.thinking += text; node.rev++; }
        return node;
      }
      /* [5702 · C-1] 主模型思考：**每 run+round 一个节点**（思考-only 的轮也建），
         不能再用「最近一条 assistant」（跨轮串）也不能只靠 pending
         （否则思考-only 的轮会把思考并到下一轮）。 */
      if (ns === "live") {
        const sn = streamNode(runId, toInt(rec.round, 0), seq, ns, modelOf(rec, data));
        sn.reasoning = (sn.reasoning || "") + text;
        sn.rev++;
        return sn;
      }
      const sn = byKey.get("stream:" + (runId || "?") + ":" + toInt(rec.round, 0));
      if (sn) { sn.reasoning = (sn.reasoning || "") + text; sn.rev++; return sn; }
      appendReasoning(text);                    // 落盘历史：并入最近一条 assistant
      return null;
    }

    const node = push(Object.assign({}, base)); // chain / sys / injection
    if (type === "done" || type === "error") flushPendingReasoning();
    return node;
  }

  function rebuild(items) {
    byKey = new Map();
    order = [];
    seen = new Set();
    ord = 0;
    structRev++;
    pendingLocalKey = null;
    pendingReasoning = "";
    pendingReasoningOrd = 0;
    snapCache = null;
    snapCacheRev = -1;
    if (Array.isArray(items)) for (const rec of items) applyRecord(rec);
    return snapshot();
  }

  /** 丢弃全部**未被认领**的本地节点（乐观上屏的临时气泡）——发送失败时回退用。
      已被真实 run 认领的节点属正式轮，绝不误伤。 */
  function dropLocal() {
    const keepKeys = order.filter((k) => {
      const n = byKey.get(k);
      if (String(n.ns || "") !== "local") return true;
      return n.claimed === true;
    });
    if (keepKeys.length === order.length && !pendingLocalKey) return false;
    const keep = new Set(keepKeys);
    for (const k of Array.from(byKey.keys())) if (!keep.has(k)) byKey.delete(k);
    order = keepKeys;
    pendingLocalKey = null;
    structRev++;
    return true;
  }

  /** 快照：结构未变 ⇒ 返回**同一引用**（渲染层据此零重排）。 */
  function snapshot() {
    if (snapCache && snapCacheRev === structRev) return snapCache;
    snapCache = { order, nodes: byKey, rev: structRev };
    snapCacheRev = structRev;
    return snapCache;
  }

  return { applyRecord, rebuild, snapshot, dropLocal };
}
