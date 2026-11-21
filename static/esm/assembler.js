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
  // [2026-10-26] 补齐新增卡间消息类型（不补会落到 sys 兜底 ⇒ 样式全丢）
  msg_ask: { kind: "cardmsg" }, msg_answer: { kind: "cardmsg" }, msg_notify: { kind: "cardmsg" },
  msg_sync: { kind: "cardmsg" }, msg_report: { kind: "cardmsg" },
  msg_directive: { kind: "cardmsg" }, coder_done: { kind: "cardmsg" }, worker_done: { kind: "cardmsg" },

  tool_call:     { kind: "tool", merge: "call" },   // 同 key 合体
  tool_result:   { kind: "tool", merge: "result" },

  judgment: { kind: "chain" }, plan_created: { kind: "chain" },
  step_start: { kind: "chain" }, step_advanced: { kind: "chain" },

  seats_enter: { kind: "seats" }, seat_start: { kind: "seats", merge: "member" },
  seat_end: { kind: "seats", merge: "member" },

  synthesize: { kind: "sys" }, deliver: { kind: "sys" }, proposal: { kind: "sys" },
  done: { kind: "sys" }, error: { kind: "sys" }, rescue: { kind: "sys" },
  rescue_refused: { kind: "sys" }, pseudo_call_unparsed: { kind: "sys" },
  pseudo_receipt: { kind: "sys" }, text_proto_refused: { kind: "sys" },
  card_idle: { kind: "sys" },
  // [断言检出 harness 化 2026-11-10] 回复断言检出的机械事实行（频次可见性）

  card_op: { kind: "sys" }, context: { kind: "sys" }, round_start: { kind: "sys" },
  round_end: { kind: "sys" }, run_start: { kind: "sys" },
  // [角色机制 5710] 角色巡检判词 / 警报
  role_judged: { kind: "sys" }, role_alarm: { kind: "sys" },

  context_preamble: { kind: "injection" }, context_injection: { kind: "injection" },
  judgment_protocol: { kind: "injection" }, context_seat_identity: { kind: "injection" },
  status_update_task: { kind: "injection" },
  // [需求确定按钮改造 2026-10-29] 【需求确定】两种专用回合的轮首任务注入：
  // 根卡那一轮直接改正稿、其他卡那一轮拟草稿后开评审。都当注入行渲染，
  // 让「这一轮在干什么」直接出现在对话里。
  requirements_final_task: { kind: "injection" },
  requirements_draft_task: { kind: "injection" },

  reasoning: { kind: "reasoning" },
  /* [5702 · C-1 2026-10-20] 逐字流增量直接进装配器（dsh `assistant/chunk` 语义）。
     此前它们只写旁挂 live DOM、round_start 一清就没——「中间轮文字被吞」的直接原因。 */
  reasoning_chunk: { kind: "reasoning" },

  // 真实落盘数据里存在、但不在任务书清单里的三类（见旧版注释）：
  card_message: { kind: "sys" }, triage_verdict: { kind: "sys" }, ping: { kind: "sys" },
};

/** revoke 是控制位记录：不渲染成节点，只驱动软删。 */
const CONTROL_TYPE = "revoke";

/* [占位思考过滤 2026-11-05] echo_fill 端点的 wire 兜底占位串（后端
   REASONING_PLACEHOLDER 同文案）可能被端点/模型回吐成"思考"并进事件流——
   它是格式件不是思考，归位时剥掉；存量脏数据一并遮蔽。 */
const REASONING_PLACEHOLDER = "(系统注入的消息，无模型思考记录)";

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
  /** [5715] 按 (run, round) 分桶的待认领思考：key＝`${run}|${round}`。
   *  一轮一个桶——归位时只允许**同 run 同轮** 的 assistant 吸收自己那桶。 */
  let pendingBuckets = new Map();
  let snapCache = null;
  let snapCacheRev = -1;

  /** [2026-11-09 · 用户令「全部按时间」] 可见序＝(ts, 到达序) 的纯函数：
      位置由**数据自己**（事件时间戳）决定，不由送达时机决定。
      ts 缺失的节点（本地乐观气泡、兜底思考）按 +∞ 落尾——它们本来就是
      「最新还没定稿」的东西。同 ts 并列时后到者排后（ord 决胜）。 */
  function tsKey(node) {
    const t = Date.parse((node && node.ts) || "");
    return Number.isFinite(t) ? t : Infinity;
  }

  /* ── 前插批次态（prepend 专属；null＝普通追加路径）─────────────────
     普通路径语义「到达序＝真实时序」在前插下不成立：旧页记录**真实时序更早**
     但到达更晚。批次里记：
       created —— 本批新建的节点键（revoke 只罩这些，不碰已装窗口）；
       texts   —— 前补文本缓冲（思考/席位思考按段序插到既有内容**之前**）；
       segs    —— 前补贡献段缓冲（同上）。 */
  let _pre = null;

  /** 把 node.key 按 tsKey 插进 order 的时隙（从尾向前找第一个 ≤ 它的位置）。
      无 ts 的节点锚定**当前尾部**（纯追加语义，保持服务端落盘序）——
      乐观气泡因此锚在「现在」，而迟到的旧帧会插到它**前面**。
      前插批次改用「同 ts 插到既有之前」：旧页记录真实时序更早，
      等时戳并列应落在已装节点前（与全量重建序一致）。 */
  function place(node) {
    let pk = tsKey(node);
    if (pk === Infinity) {
      pk = order.length ? tsKey(byKey.get(order[order.length - 1])) : 0;
    }
    if (_pre) {
      /* [热路径·任务二收尾] 前插**延迟归并**：不再逐条 slice+concat
         （那是 O(新增×已装) 的全量复制）。每条只记 {node,pk,tail,seq}=O(1)，
         位置到 `_preFlush` 一次归并时才算——归并语义与旧逐条插入完全一致：
         pk 小在前；同 pk 本批先于既有；批内同 pk 按到达序（seq 稳定）。 */
      _pre.pending.push({ node, pk, seq: ++_pre.pseq });
      return;
    }
    let i = order.length;
    while (i > 0 && tsKey(byKey.get(order[i - 1])) > pk) i--;
    order = order.slice(0, i).concat([node.key], order.slice(i));
    structRev++;
  }

  /** 把本批 pending 一次性归并进 order（单次 O(已装+新增) 通道）。
      任何对 order 的**结构性读取**（按位置扫描/截取/过滤）都要先 flush——
      读侧在函数入口调它，批内新节点对配对/撤销/兜底逻辑保持可见。 */
  function _preFlush() {
    if (!_pre || !_pre.pending.length) return;
    const pend = _pre.pending;
    pend.sort((a, b) => (a.pk - b.pk) || (a.seq - b.seq));
    _pre.copied = (_pre.copied || 0) + order.length + pend.length;
    /* 快路径：整批都早于已装首节点 ⇒ 直接前拼，免逐元素比对。 */
    if (!order.length || pend[pend.length - 1].pk < tsKey(byKey.get(order[0]))) {
      order = pend.map((p) => p.node.key).concat(order);
      _pre.pending = [];
      structRev++;
      return;
    }
    const merged = new Array(order.length + pend.length);
    let pi = 0, oi = 0, mi = 0;
    while (pi < pend.length || oi < order.length) {
      if (oi >= order.length) { merged[mi++] = pend[pi++].node.key; continue; }
      if (pi >= pend.length) { merged[mi++] = order[oi++]; continue; }
      const p = pend[pi];
      _pre.scanned++;
      const ct = tsKey(byKey.get(order[oi]));
      /* 同 pk：批内新节点插到「非本批」既有之前、本批已归并者之后——
         与原逐条插入的 created 判据等价（中途 flush 过后次序仍不倒插）。 */
      if (p.pk < ct || (p.pk === ct && !_pre.created.has(order[oi]))) {
        merged[mi++] = p.node.key; pi++;
      } else merged[mi++] = order[oi++];
    }
    /* 批内已被撤下的 pending（purgeFrom 先 flush 后删，正常到不了这；
       byKey 复核兜底防 re-key/删除残留把死键归并回去）。 */
    order = merged.filter((k) => byKey.has(k));
    _pre.pending = [];
    structRev++;
  }

  /** 归并通道搬移量（验收对账）：copied = 引用搬移的元素数，每次 flush 一次累加。 */

  function push(node) {
    node.ord = ++ord;
    node.rev = 1;
    byKey.set(node.key, node);
    if (_pre) _pre.created.add(node.key);
    place(node);
    return node;
  }

  /** 文本累加：追加路径照旧 ``obj[field] += text``；前插批次入缓冲，
      批次尾统一「批文本 + 既有」前补（段序保持，不会倒插）。 */
  function _accText(obj, field, text) {
    if (!text) return;
    if (_pre) {
      let rec = _pre.texts.get(obj);
      if (!rec) { rec = {}; _pre.texts.set(obj, rec); }
      (rec[field] = rec[field] || []).push(text);
    } else {
      obj[field] = (obj[field] || "") + text;
    }
  }

  /** 贡献段累加：同 _accText 的前补语义。 */
  function _accSegs(node, seg) {
    if (!seg) return;
    if (_pre) {
      let arr = _pre.segs.get(node);
      if (!arr) { arr = []; _pre.segs.set(node, arr); }
      arr.push(seg);
    } else {
      (node._reasoningSegs = node._reasoningSegs || []).push(seg);
    }
  }

  /** 取/建**本 run+round 的流式 assistant 节点**（键 `stream:{run}:{round}`）。
      [5702 · C-1] 正文与思考都走这里：dsh 的 assistant-step 以 step 为节点，
      「只有思考、没有正文」的 step 也算可见内容（assistant.ts hasVisibleContent）。
      若只在 content_chunk 建节点，思考-only 的轮会把思考并到下一轮（实测踩到）。 */
  function streamNode(runId, roundNo, seq, ns, model, ts) {
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
      /* [前插] 节点带上记录 ts——无 ts 会按「锚定尾部」落到已装窗口之后，
         旧页思考/正文就插错位置了。 */
      ts: String(ts || ""),
    };
    /* [5715] 落盘历史里先到的思考随节点归位（不丢）：只取**同 (run, round)** 那一桶。 */
    const bkey = bucketKey(runId, roundNo);
    const bkt = pendingBuckets.get(bkey);
    if (bkt) {
      node.reasoning = bkt.text;
      _absorbReasoningSegs(node, bkt.segs);
      pendingBuckets.delete(bkey);
    }
    byKey.set(skey, node);
    if (_pre) _pre.created.add(node.key);
    place(node);
    return node;
  }

  /** 最近一条 assistant 节点（主模型思考的归属目标）。
   *
   *  [5715 · 修] 归属**精确到 run + round**（与 live 路径的 ``stream:{run}:{round}``
   *  同源）。原实现只按「最近一条 assistant」找，**既不限 run 也不限轮**：
   *   · 跨 run：后一个 run 的 thinking 挂到前一个 run 的消息上（实测长链 run 的
   *     247 条思考全挂到 21:26 那条老消息下）⇒ 刷新后那整段思考位置全错、丢失；
   *   · 同 run 跨轮：第 1 轮的思考并到第 2 轮的回复上（实测：第 1 轮思考整个消失）。
   *  ⇒ 根子是 **disk 路径没有「每轮一个 assistant 节点」的概念**，与 live 不同源。
   *  现在 disk 路径也每轮落一条 assistant 节点（后端 ``_land_round_reply``），
   *  这里就能按 (run, round) 精确归位。
   *
   *  口径缺失（老数据没有 run_id 或 round）时退回「同 run 最近一条」，保持既有行为。 */
  function lastAssistant(runId, round) {
    /* [热路径] 不 flush：order 里的节点都比 pending 新——倒扫 order 优先；
       未命中再倒扫 pending（同页先于本记录的轮首节点要能命中）。 */
    const hasRound = round !== undefined && round !== null && round !== "";
    const match = (n) => {
      if (!n || n.kind !== "assistant") return false;
      if (runId && n.run_id && n.run_id !== runId) return false;
      if (hasRound && n.round !== undefined && n.round !== null && n.round !== ""
          && Number(n.round) === Number(round)) return true;
      return !!(runId && n.run_id === runId && !hasRound);
    };
    for (let i = order.length - 1; i >= 0; i--) if (match(byKey.get(order[i]))) return byKey.get(order[i]);
    if (_pre) for (let i = _pre.pending.length - 1; i >= 0; i--) if (match(_pre.pending[i].node)) return _pre.pending[i].node;
    return null;
  }

  /** 同 run 的 seats 节点（三席席卡；懒建）。 */
  function seatsOf(runId, rec) {
    const match = (n) => n && n.kind === "seats" &&
      (!runId || String(n.run_id || "") === runId);
    for (let i = order.length - 1; i >= 0; i--) {
      const n = byKey.get(order[i]);
      if (match(n)) return n;
    }
    if (_pre) for (let i = _pre.pending.length - 1; i >= 0; i--) {
      if (match(_pre.pending[i].node)) return _pre.pending[i].node;
    }
    const seq = toInt(rec.seq, 0);
    const ns = rec.ns || "disk";
    return push({
      seq, ns, type: rec.type, kind: "seats", round: rec.round, run_id: runId,
      data: rec.data || {}, model: modelOf(rec, rec.data),
      members: {}, entered: false,
      key: keyOf(ns, seq, "seats"),
      ts: String(rec.ts || ""),             // [前插] 同 streamNode：带 ts 落位
    });
  }

  function memberOf(seats, model) {
    const key = String(model || "");
    if (!key) return null;
    if (!seats.members[key]) seats.members[key] = { model: key, thinking: "", start: null, end: null };
    return seats.members[key];
  }

  /* [窄投影·修 2026-11-21] ``_reasoningSegs`` 记**全部**贡献段
     ``{seq, text, brief}``（按到达序）：brief 段展开时按 seq 回取全文后
     **按段序重拼**——不能用字符串替换（两段预览相同会拼错位置）；
     撤销段由详情回包的 ``revoked`` 标出、换占位标记，不再留旧预览冒充全文。 */
  function appendReasoning(text, runId, round, seg) {
    if (!text) return;
    const target = lastAssistant(runId, round);
    if (target) {
      _accText(target, "reasoning", text);
      _accSegs(target, seg);
      target.rev++;
    } else {
      /* [5715] 按 (run, round) 分桶暂存：归位时只允许**同 run 同轮** 的 assistant
         吸收（原来是一个字符串 ⇒ 轮 1 的思考被轮 2 的回复吸收，跨轮串）。 */
      const key = bucketKey(runId, round);
      const cur = pendingBuckets.get(key);
      const segs = (cur && cur.segs) || [];
      if (seg) segs.push(seg);
      pendingBuckets.set(key, {
        text: (cur ? cur.text : "") + text,
        ord: cur ? cur.ord : (ord + 1),
        segs,
      });
    }
  }

  /** (run, round) 分桶键。round 缺失时用空串（老数据按「同 run 最近一条」兼容）。 */
  function bucketKey(runId, round) {
    const r = (round === undefined || round === null) ? "" : String(round);
    return String(runId || "") + "|" + r;
  }

  /** [窄投影] 桶里暂存的贡献段归位到节点（保持到达序）。 */
  function _absorbReasoningSegs(node, segs) {
    if (!segs || !segs.length) return;
    const list = (node._reasoningSegs = node._reasoningSegs || []);
    for (const s of segs) list.push(s);
  }

  /** 该轮收口：思考若始终无人认领，落一条兜底节点（不丢文本）。**每个残余桶一条**。
      [2026-11-09 · 用户令「全部按时间」] 兜底节点挂**当前尾部节点的 ts**——
      它的真实时间就是「收口时被发现」这一刻，按 tsKey 自然落在收口处，
      不再用旧的 ord 插队语义（resort 随此退役）。 */
  function flushPendingReasoning(runId, round) {
    if (!pendingBuckets.size) return;
    /* 不 flush：pending 只会前插，尾部仍是 order 尾——兜底锚点不受影响。 */
    const tailKey = order[order.length - 1];
    const tail = tailKey ? byKey.get(tailKey) : null;
    const seq = tail ? tail.seq : 0;
    const ns = (tail && tail.ns) || "disk";
    for (const b of pendingBuckets.values()) {
      const node = {
        seq, ns, type: "reasoning", kind: "sys", round: tail ? tail.round : 0, run_id: runId || "",
        data: { text: b.text, orphan: true }, model: "",
        key: String(ns) + ":orphan:" + toInt(seq, 0) + ":" + toInt(b.ord, 0),
        rev: 1,
        ord: b.ord,
        ts: (tail && tail.ts) || "",
      };
      _absorbReasoningSegs(node, b.segs);
      if (!byKey.has(node.key)) {
        byKey.set(node.key, node);
        if (_pre) _pre.created.add(node.key);
        place(node);
      }
    }
    pendingBuckets = new Map();
  }

  /** tool_result 的配对目标：tool_call_id 精确优先，其次最近未闭合调用行。
   *  [5715] 限定**同一个 run**（原来不限 ⇒ 后一个 run 的 result 配到前一个 run 的
   *  call 上 ⇒ 工具与内容张冠李戴，刷新后读不出「谁调了什么、结果是什么」）。 */
  function toolTargetOf(d, runId) {
    /* [热路径] 不 flush：pending 批内节点（本页已装、位次未定）同样可配对——
       按「时序更早」语义它们只可能是候选中的次选，order 扫描优先，
       未命中再倒扫 pending（数组小、O(页内) 不 O(历史)）。 */
    const id = d && (d.tool_call_id || d.call_id);
    const pend = _pre && _pre.pending;
    if (id) {
      for (let i = order.length - 1; i >= 0; i--) {
        const n = byKey.get(order[i]);
        if (!n || n.kind !== "tool" || !n.call) continue;
        if (runId && n.run_id && n.run_id !== runId) continue;   // 跨 run 不认
        if ((n.call.tool_call_id || n.call.call_id) === id) return n;
      }
      if (pend) for (let i = pend.length - 1; i >= 0; i--) {
        const n = pend[i].node;
        if (!n || n.kind !== "tool" || !n.call) continue;
        if (runId && n.run_id && n.run_id !== runId) continue;
        if ((n.call.tool_call_id || n.call.call_id) === id) return n;
      }
    }
    for (let i = order.length - 1; i >= 0; i--) {
      const n = byKey.get(order[i]);
      if (!n || n.kind !== "tool" || n.closed) continue;
      if (runId && n.run_id && n.run_id !== runId) continue;   // 跨 run 不认
      return n;
    }
    if (pend) for (let i = pend.length - 1; i >= 0; i--) {
      const n = pend[i].node;
      if (!n || n.kind !== "tool" || n.closed) continue;
      if (runId && n.run_id && n.run_id !== runId) continue;
      return n;
    }
    return null;
  }

  /** 前插反向配对：结果在已装窗口（``call===null`` 的孤儿行）而调用在旧页
      ⇒ 按 tool_call_id 精确找该行；缺 call_id 的旧记录按全量重建的对偶语义
      兜底——重建是「结果配最近未闭合的前置调用」，前插即「调用配 seq 紧随其后的
      首个未认领孤儿结果」（孤儿被认领后 call 非空，自然不再二次命中）。 */
  function _preResultTarget(d, runId, seq) {
    /* [热路径] 不 flush：孤儿结果在 pending 里同样要被调用认领——
       pending 位次全部先于 order，先扫 pending 再扫 order 保「首个 seq>call」序。 */
    const id = d && (d.tool_call_id || d.call_id);
    if (_pre) {
      let best = null;
      for (const p of _pre.pending) {
        const n = p.node;
        if (!n || n.kind !== "tool" || n.call || !n.result) continue;
        if (runId && n.run_id && n.run_id !== runId) continue;
        if (id) {
          if ((n.result.tool_call_id || n.result.call_id) === id) return n;
        } else if (toInt(n.seq, 0) > seq &&
                   (!best || p.pk < best.pk || (p.pk === best.pk && p.seq < best.seq))) {
          best = p;                                  // 位次最前的候选孤儿
        }
      }
      if (best) return best.node;
    }
    for (let i = 0; i < order.length; i++) {
      const n = byKey.get(order[i]);
      if (!n || n.kind !== "tool" || n.call || !n.result) continue;
      if (runId && n.run_id && n.run_id !== runId) continue;
      if (id) {
        if ((n.result.tool_call_id || n.result.call_id) === id) return n;
      } else if (toInt(n.seq, 0) > seq) return n;
    }
    return null;
  }

  /** 软删：revoke{from_seq:F} 撤下此刻已装配的 seq>=F 节点。
      前插批次域收窄到**本批新建**：revoke 落在旧页时，它发生时点上
      已装窗口的记录尚未写入（seq 全在它之后）——绝不可一并罩掉。 */
  function purgeFrom(fromSeq) {
    /* [热路径] 不 flush：pending 里本批新建且 seq>=F 的节点同样罩掉——
       从 pending 账和 byKey 双删，归并时不再出现。 */
    if (_pre && _pre.pending.length) {
      _pre.pending = _pre.pending.filter((p) => {
        if (toInt(p.node.seq, 0) < fromSeq || !_pre.created.has(p.node.key)) return true;
        byKey.delete(p.node.key);
        _pre.created.delete(p.node.key);
        return false;
      });
    }
    const keepKeys = order.filter((k) => {
      const n = byKey.get(k);
      if (toInt(n.seq, 0) < fromSeq) return true;
      return _pre ? !_pre.created.has(k) : false;
    });
    if (keepKeys.length === order.length) return;
    const keep = new Set(keepKeys);
    for (const k of Array.from(byKey.keys())) if (!keep.has(k)) byKey.delete(k);
    order = keepKeys;
    pendingBuckets = new Map();
    if (_pre) { _pre.frontier = null; _pre.lastPk = -Infinity; }  // order 变了，游标作废
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
      const node = streamNode(runId, toInt(rec.round, 0), seq, ns, modelOf(rec, data), rec.ts);
      node.data.text = String(node.data.text || "") + text;
      return node;
    }

    const kind = kindOf(type);
    const base = {
      seq, ns, type, kind, round: toInt(rec.round, 0), run_id: runId,
      data, model: modelOf(rec, data), key: keyOf(ns, seq, kind),
      /* [窄投影 2026-11-21 · 性能批次二] brief 记录 → 节点带契约位：
         渲染层据此在「点开详情」时按 seq 回取原文（timeline/event）。 */
      _brief: !!rec._brief,
      /* [对话界面商业化 2026-10-17] 事件时间随节点走：落盘记录带 ts（ISO），
         实时帧带 ts（秒 float，已由 frameToRecord 归一）。旧记录无 ts ⇒ 空串，
         渲染层按「有则显示」处理，不做任何猜测性补值。 */
      ts: String(rec.ts || ""),
    };

    /* [5715 · P19 轮次出现两次] 同一轮的 round_start **只留一个节点**。
       实测（work_025/card_054，2026-10-23）：wb_exec 委派路径自己发一条 round_start
       （wb_exec/bridge.py，phase=wb_exec，带 executor/model），委派失败回落系统路径
       又发一条（phase=main/tools）⇒ **同一轮两条** ⇒ 界面两行「第 N 轮」。
       这里给 round_start 一个 (run_id, round) 稳定键：同键合流（后到的更新数据、
       **不新增节点**）⇒ 无论上游发几条、无论新旧历史数据，界面一轮一行。
       没有 run_id 的老数据**不按轮合流**（否则不同 run 的同轮号会被吞成一条）。 */
    if (type === "round_start") {
      const roundNo = toInt(rec.round !== undefined ? rec.round : data.round, 0);
      const rkey = runId ? "round:" + runId + ":" + roundNo : base.key;
      const exist = byKey.get(rkey);
      if (exist) {
        exist.data = Object.assign({}, exist.data, data);
        exist.rev++;
        return exist;
      }
      base.key = rkey;
      return push(Object.assign({}, base));
    }

    if (kind === "user" || kind === "assistant" || kind === "cardmsg") {
      /* [5715] **disk 路径与 live 路径同构**：assistant 节点键一律用
         `stream:{run_id}:{round}`（不再用 `keyOf(ns, seq, kind)`）。
         live 路径本来就是这个键（content_chunk 建节点、assistant_text 定稿同节点）⇒
         落盘历史用同一个键，两条路径的节点**完全可互换**：
           · 轮首落一条（正文空）⇒ 建节点，位置＝轮首（工具之前）；
           · 轮末落一条（正文全）⇒ 定稿同节点，位置不变、内容更新；
           · 该轮的思考按 (run, round) 桶归到这个节点上 ⇒ 思考在工具**之前**，
             与活体完全一致。
         老数据没有「轮首那条」⇒ 走下面的定稿分支找不到节点 ⇒ 退回原 keyOf 路径
         （老记录照常渲染，位置＝轮末——保持既有行为，不回归）。 */
      if (kind === "assistant" && !isLocal) {
        const skey = "stream:" + (runId || "?") + ":" + toInt(rec.round, 0);
        const sn = byKey.get(skey);
        if (sn) {
          // 同 run+round 已建节点 ⇒ 定稿（内容优先，text 兜底）
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
        // 轮首那条：建节点，键＝stream:{run}:{round}（与 live 同键）
        base.key = skey;
        base.data = Object.assign({}, data, { text: (data && data.text) || "" });
        const node = push(Object.assign({}, base));
        const bkey = bucketKey(node.run_id || runId, node.round);
        const bkt = pendingBuckets.get(bkey);
        if (bkt) {
          node.reasoning = bkt.text;
          _absorbReasoningSegs(node, bkt.segs);
          node.rev++;
          pendingBuckets.delete(bkey);
        }
        return node;
      }
      if (kind === "user") {
        // 轮界：上一轮的兜底思考先落位
        flushPendingReasoning();
        // 乐观上屏的 pending 节点：真实 user_message 到达 ⇒ 原地升级（不出双气泡）
        const _pendScan = (arr, get) => {
          for (let i = arr.length - 1; i >= 0; i--) {
            const n = get(arr[i]);
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
          return null;
        };
        const up = _pendScan(order, (k) => byKey.get(k))
          || (_pre && _pendScan(_pre.pending, (p) => p.node));
        if (up) return up;
      }
      /* [5702 · C-1] 落盘 assistant_text 的**就地定稿**已并入上面那段
         （[5715] disk 与 live 同键、同定稿语义）；此处保留 push 路径，
         供**老数据**（没有「轮首那条」⇒ 上面找不到同键节点）走原 keyOf 键。 */
      const node = push(Object.assign({}, base));
      if (kind === "user" && isLocal) {
        node.pending = true;
        node.localKey = rec.local_key;
        pendingLocalKey = node.key;
      }
      if (kind === "assistant") {
        node.reasoning = "";
        /* [5715] 只吸收**同 (run, round)** 桶里的思考。
           原来不限 run 不限轮 ⇒ 长链 run 的思考被前一个 run 的消息认领（实测 247 条
           全挂到 21:26 那条老消息下）、同 run 内轮 1 的思考被轮 2 的回复吸收
           ⇒ 刷新后「工具与正文之间那段模型在想什么」整段丢失、位置全错。 */
        const bkey = bucketKey(node.run_id || runId, node.round !== undefined ? node.round : rec.round);
        const bkt = pendingBuckets.get(bkey);
        if (bkt) {
          node.reasoning = bkt.text;
          _absorbReasoningSegs(node, bkt.segs);
          node.rev++;
          pendingBuckets.delete(bkey);
        }
      }
      return node;
    }

    if (kind === "tool") {
      const merge = REGISTRY[type].merge;
      if (merge === "call") {
        /* [前插·跨页配对] 调用在旧页、结果已在已装窗口 ⇒ 孤儿行被认领。
           与全量重建**同形态**：节点键/seq/ts 换调用的（重建时节点生在调用处），
           行从结果处拔起按调用 ts 重落位——顺序、节点数、详情键三者对齐重建。 */
        if (_pre) {
          const hit = _preResultTarget(data, runId, seq);
          if (hit) {
            byKey.delete(hit.key);
            order = order.filter((k) => k !== hit.key);
            _pre.pending = _pre.pending.filter((p) => p.node !== hit);
            hit.key = base.key;                    // keyOf(ns, 调用seq, "tool")
            hit.seq = seq;
            hit.ts = String(rec.ts || "");
            /* 字段级对齐重建：节点身份字段取自调用记录（ns/run/round/model/
               _brief），data/type/result/closed/_result* 已是结果侧原样。 */
            hit.ns = ns; hit.run_id = runId; hit.round = base.round;
            hit.model = base.model; hit._brief = base._brief;
            hit.call = data;
            hit.rev++;
            byKey.set(hit.key, hit);
            _pre.created.add(hit.key);             // 新键 ⇒ added（旧键随 order 差集卸载）
            place(hit);
            return hit;
          }
        }
        return push(Object.assign({}, base, { call: data, result: null, closed: false }));
      }
      const target = toolTargetOf(data, runId);
      if (target) {
        target.result = data;
        target.closed = true;
        target.type = "tool_result";
        target.data = data;
        /* [窄投影] result 字段被预览化时记下取回键——展开时按它补全文。 */
        target._resultSeq = seq;
        target._resultBrief = !!rec._brief;
        target.rev++;
        return target;
      }
      /* 孤儿结果（调用未装/不在本批）：详情回取键现在就带上——旧页调用随后
         前插认领时只换位置和 call 字段，_resultSeq/_resultBrief 不再丢。 */
      return push(Object.assign({}, base, {
        call: null, result: data, closed: true,
        _resultSeq: seq, _resultBrief: !!rec._brief,
      }));
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
      const text = String(data.text || "").split(REASONING_PLACEHOLDER).join("");
      if (model) {                              // 三席思考 → 席卡
        const node = seatsOf(runId, rec);
        const m = memberOf(node, model);
        if (m) { _accText(m, "thinking", text); node.rev++; }
        return node;
      }
      /* [5702 · C-1] 主模型思考：**每 run+round 一个节点**（思考-only 的轮也建），
         不能再用「最近一条 assistant」（跨轮串）也不能只靠 pending
         （否则思考-only 的轮会把思考并到下一轮）。
         [5715] **disk 与 live 彻底同构**：两条路径都用 ``stream:{run}:{round}`` 这个键
         建 assistant 节点 ⇒
           · 节点位置＝思考到达处（工具**之前**）⇒ 与活体逐点一致；
           · 思考挂在本轮自己的节点上 ⇒ 不会跨 run、不会跨轮串；
           · 该轮后续的 assistant_text 事件**定稿**同一个节点（内容更新、位置不变）。
         老数据（round 缺失/为 0，多轮挤同一键）仍走 pending 兜底，不回归。 */
      const roundNo = toInt(rec.round, 0);
      /* [窄投影·修] 记**全部**贡献段（{seq, text, brief}，到达序）：展开时
         brief 段按 seq 取回原文、按段序重拼（两段预览相同也不会拼错位）。 */
      const trackSeg = (n) => {
        _accSegs(n, { seq, text, brief: !!rec._brief });
      };
      if (ns === "live" || roundNo > 0) {
        const sn = streamNode(runId, roundNo, seq, ns, modelOf(rec, data), rec.ts);
        _accText(sn, "reasoning", text);
        trackSeg(sn);
        sn.rev++;
        return sn;
      }
      const sn = byKey.get("stream:" + (runId || "?") + ":" + toInt(rec.round, 0));
      if (sn) {
        _accText(sn, "reasoning", text);
        trackSeg(sn);
        sn.rev++;
        return sn;
      }
      appendReasoning(text, runId, toInt(rec.round, 0),
        { seq, text, brief: !!rec._brief });  // 落盘历史：并入**同 run 同轮**的 assistant
      return null;
    }

    const node = push(Object.assign({}, base)); // chain / sys / injection
    if (type === "done" || type === "error") flushPendingReasoning(runId, toInt(rec.round, 0));
    return node;
  }

  /* ── 前插增量装配（2026-11-22 · 性能批次二·任务二）────────────────
     上翻取回的**旧页**记录走这里：不重装全窗口，只 apply 本批记录——
       · 新建节点按 ts 落位（同 ts 插到既有之前）；
       · 命中既有节点的合并（工具配对/round_start 合流/思考归位）记 touched；
       · 思考与席位思考**前补**（段序保持：批内按序，批先于已装）；
       · 本批内的 revoke 只罩本批新建（时点语义，不碰已装窗口）；
       · 重复记录由 seen 去重（页界重叠安全）。
     返回 {added:[新键], touched:[被改的既有键], records} 供渲染层只挂新行。 */
  function prepend(items) {
    const list = Array.isArray(items) ? items : [];
    if (!list.length) return { added: [], touched: [], records: 0 };
    _pre = { created: new Set(), texts: new Map(), segs: new Map(),
             pending: [], pseq: 0, scanned: 0 };
    const touched = new Set();
    const t0 = (typeof performance !== "undefined") ? performance.now() : 0;
    try {
      for (const rec of list) {
        const n = applyRecord(rec);
        if (n && n.key && !_pre.created.has(n.key)) touched.add(n.key);
      }
    } finally {
      /* 前补文本落账：批内顺序 join + 既有内容在后（段序保持）。
         对象字段级记账（node.reasoning / member.thinking 同器）。 */
      for (const [obj, rec] of _pre.texts) {
        for (const f of Object.keys(rec)) {
          obj[f] = rec[f].join("") + String(obj[f] || "");
        }
      }
      for (const [node, arr] of _pre.segs) {
        node._reasoningSegs = arr.concat(node._reasoningSegs || []);
      }
      _preFlush();                     // 收尾归并：pending 一次性落 order
      const batch = _pre;
      _pre = null;
      structRev++;                     // touched 也要让快照缓存失效
      return {
        added: order.filter((k) => batch.created.has(k)),
        touched: Array.from(touched),
        records: list.length,
        scanned: batch.scanned,            // 归并通道的实际比对步数（热路径对账）
        copied: batch.copied || 0,           // 引用搬移量（验收对账：单次数组通道）
        ms: (typeof performance !== "undefined") ? performance.now() - t0 : 0,
      };
    }
  }

  function rebuild(items) {
    byKey = new Map();
    order = [];
    seen = new Set();
    ord = 0;
    structRev++;
    pendingLocalKey = null;
    pendingBuckets = new Map();
    snapCache = null;
    snapCacheRev = -1;
    if (Array.isArray(items)) for (const rec of items) applyRecord(rec);
    return snapshot();
  }

  /** 丢弃全部**未被认领**的本地节点（乐观上屏的临时气泡）——发送失败时回退用。
      已被真实 run 认领的节点属正式轮，绝不误伤。 */
  function dropLocal() {
    if (_pre) _preFlush();
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
    if (_pre) _preFlush();                      // 批中快照必须含已归并的 pending
    if (snapCache && snapCacheRev === structRev) return snapCache;
    snapCache = { order, nodes: byKey, rev: structRev };
    snapCacheRev = structRev;
    return snapCache;
  }

  return { applyRecord, rebuild, prepend, snapshot, dropLocal };
}
