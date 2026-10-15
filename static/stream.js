/* stream.js - [会话渲染单源化 2026-10-15] 单一事件流 → 装配器 → 节点快照 → 快照渲染。
 *
 *   用户裁定（任务书《会话渲染单源化》C1–C6）：
 *     C1 单源单通道：timeline（落盘事件流）是**唯一**渲染数据源；/session 的
 *        消息渲染通道（workspace.renderChat）已退役，本文件不读它。
 *     C2 位置由数据自带：节点位置＝(分组键, 到达序)，渲染**只读快照**。
 *        旧缝合族的游标计数、插入锚点、思考归位回扫、消息跳过表、组封存
 *        五件套已**整体删除**（不是注释掉）；本文件不再有任何一处读 DOM
 *        现状做位置推算（红线6 的 grep 判据＝0）。
 *     C4 缝合族删净，不是注释掉。
 *     C5 未知事件类型不丢：落 sys 兜底行「未识别事件」。
 *     C6 过程折叠：组容器带收起/展开开关（默认展开，理由见 addGroupToggle）。
 *
 *   ── 数据流（刷新与实时同一条）──
 *     刷新/换卡：GET /timeline → assembler.rebuild(items) → snapshot() → renderSnapshot
 *     实时：    /stream 帧 → frameToRecord() → assembler.applyRecord() →
 *               **只重画受影响的那一个轮组**（renderGroup）
 *     结算：    POST 返回（此时事件已全部落盘）→ resync() 走回第一条路
 *     /tokens 逐字流**不进装配器**（content_chunk 是过程不是记录）：仍实时写
 *               live 正文气泡；assistant_text 落盘后由快照转正式节点。
 *
 *   ★ 分组键：轮次锚取 run_id（pair_id），缺失回落 round。顶层 round 不是
 *     对话轮次（run_start 置 0、每 round_start 递模型轮次），按它分组会把
 *     每轮的 round=0 前缀全并进同一组。依据与全量核对见
 *     static/esm/assembler.js 文件头与施工回报。
 *
 *   ★ 消息气泡渲染由 workspace 注入（setMessageRenderers）：内容格式化
 *     （md / 卡间信头剥离 / 元信息行）归 workspace，本文件只管**放哪**——
 *     位置是数据的函数，不是 DOM 现状的函数。
 *
 *   SSE 失败回调 onFail → 由 workspace.js 回退 createWaitingBubble 轮询（降级兜底）。
 *   事件纯展示层：只读渲染，不写存储、不影响 POST。 */
import { API } from "./esm/api.js?v=30";
import { escapeHtml as esc } from "./esm/util.js?v=30";
/* [边出边渲染 2026-10-14 · 用户令 ⑧] 流式期间也要渲染 md */
import { renderMarkdown } from "./esm/md.js?v=30";
import { createAssembler, groupKeyOf } from "./esm/assembler.js?v=30";

let current = null;   // {wid,cid,es,esT,asm,groups,liveSlot,liveRunId,textEl,textBody,reasoning,reasoningText,fallbackUsed,lastSeq,lastTokenSeq}

  function fmtData(data) {
    try { return JSON.stringify(data, null, 1); } catch (e) { return String(data); }
  }

  function logInfo(msg) {
    try { if (window.console && console.info) console.info(msg); } catch (e) { /* 日志零阻断 */ }
  }

  function clip(s, n) {
    const t = String(s == null ? "" : s);
    return t.length > n ? t.slice(0, n) + "…" : t;
  }

  /** 真实字段名值（逐个 emit 调用点实读，不是照清单编的说明） */
  function fld(k, v) {
    if (v === undefined || v === null || v === "") return "";
    return ' <span class="f">· <span class="k">' + esc(k) + "</span>=" + esc(clip(v, 80)) + "</span>";
  }

  /** 行为类型（Ⅴ：config/behavior_registry.json）→ 中文显示名 */
  function btLabel(type) {
    return String(type) === "think" ? "思考" : "执行";
  }

  /** 卡间消息类型（Ⅰ：config/message_type_registry.json）→ 中文显示名 */
  const CARD_MSG_DISPLAY = { msg_ask: "询问", msg_answer: "应答", msg_notify: "通知" };

  function chatEl() {
    return document.getElementById("chatArea");
  }

  /* [对话区会话流样式 2026-10-12] 统一内容列：轮组容器与 live 槽共用一条 .msgs。 */
  function msgsEl() {
    const ca = chatEl();
    if (!ca) return null;
    let m = ca.querySelector(":scope > .msgs");
    if (!m) {
      m = document.createElement("div");
      m.className = "msgs";
      ca.appendChild(m);
    }
    return m;
  }

  function scrollBottom() {
    const ca = chatEl();
    if (ca) ca.scrollTop = ca.scrollHeight;
  }

  /* ==================================================================
     一、纯元素构造器（无状态、不定位、不 append）
     —— 从旧 renderEvent 的 switch 拆出：原来「造行」和「插到哪」是一件事，
        那是归位缝合的根。现在造与放分离，放由快照位置决定。
     ================================================================== */

  /** 一条过程行：事件名徽标 ＋ 说明（含真实字段） ＋ 行尾状态槽。
      cls：ok/fail/err/void/content；detail：点开看的原始载荷。 */
  function makeRow(type, txtHtml, subHtml, detail, cls) {
    const row = document.createElement("div");
    row.className = "step-row" + (cls ? " " + cls : "");
    row.dataset.ev = type;
    row.innerHTML = '<span class="ev">' + esc(type) + "</span>" +
                    '<span class="txt">' + txtHtml + "</span>" +
                    (subHtml ? '<span class="sub">' + subHtml + "</span>" : "");
    if (detail) {
      const det = document.createElement("pre");
      det.className = "step-detail";
      det.textContent = detail;
      row.appendChild(det);
      row.classList.add("has-detail");
      row.onclick = (e) => {
        if (e.target.tagName === "PRE") return;
        det.style.display = det.style.display === "block" ? "none" : "block";
      };
    }
    return row;
  }

  /** 缩进行（层级用 padding 表现，不加容器元素）。 */
  function makeSubRow(type, txtHtml, subHtml, detail, cls, lv) {
    return makeRow(type, txtHtml, subHtml, detail, (cls ? cls + " " : "") + (lv || "lv2"));
  }

  /** 计划步骤清单（plan_created 的真实 steps[] 载荷）。 */
  function makePlanSteps(steps) {
    const wrap = document.createElement("div");
    wrap.className = "psteps";
    wrap.innerHTML = steps.map(s =>
      '<div class="pstep"><span class="bt">' + esc(btLabel(s.type)) + "</span>" +
      "<span>" + esc(s.id) + " · " + esc(clip(s.prompt, 70)) + "</span>" +
      '<span class="f">id=' + esc(s.id) + " criteria=" + esc(clip(s.criteria, 40)) + "</span></div>"
    ).join("");
    return wrap;
  }

  /** 主模型思考块（折叠交互与旧 .think 形态逐字一致）。 */
  function makeThink(text, opts) {
    const o = opts || {};
    const r = document.createElement("div");
    r.className = "think" + (o.done ? " done" : "") + (o.collapsed ? " collapsed" : "");
    r.innerHTML =
      '<div class="think-head">' + (o.done ? "" : '<i class="dot"></i>') +
      '<span class="think-lab">' + (o.done ? "已思考" : "思考中") + "</span>" +
      (o.time ? '<span class="think-time">' + esc(o.time) + "</span>" : "") +
      '<span class="think-caret">' + (o.collapsed ? "▸" : "▾") + "</span></div>" +
      '<div class="think-body"></div>';
    r.querySelector(".think-body").textContent = text || "";
    r.querySelector(".think-head").onclick = () => {
      const open = r.classList.toggle("collapsed");
      r.querySelector(".think-caret").textContent = open ? "▸" : "▾";
      scrollBottom();
    };
    return r;
  }

  /** 三席并排卡片（seats 节点：三席各一卡，思考与输出同卡）。 */
  function makeSeats(members) {
    const host = document.createElement("div");
    host.className = "seats";
    for (const key of Object.keys(members)) {
      const m = members[key] || {};
      const card = document.createElement("div");
      card.className = "seat-card";
      card.dataset.seat = key;
      card.innerHTML =
        '<div class="sc-head"><span class="m"></span><span class="st">思考中</span></div>' +
        '<div class="sc-body"><span class="tk"></span></div>' +
        '<div class="sc-foot"><span class="ok"></span><span class="meta"></span></div>';
      card.querySelector(".sc-head .m").textContent = m.model || key;
      card.querySelector(".tk").textContent = m.thinking || "";
      const end = m.end;
      if (end) {
        if (end.text) {
          const o = document.createElement("div");
          o.className = "ot";
          o.textContent = end.text;
          card.querySelector(".sc-body").appendChild(o);
        }
        const st = card.querySelector(".sc-head .st");
        st.textContent = (end.ok ? "✓ " : "✗ ") + "已返回";
        if (!end.ok) st.classList.add("fail");
        card.querySelector(".sc-foot .ok").textContent = "ok=" + (end.ok ? "true" : "false");
        card.querySelector(".sc-foot .meta").textContent = end.ok
          ? ((end.latency_ms || 0) / 1000).toFixed(1) + "s · " + (end.chars || 0) + "字"
          : clip(end.error || "失败", 40);
      } else if (m.start) {
        card.querySelector(".sc-head .st").textContent = "思考中";
      }
      host.appendChild(card);
    }
    return host;
  }

  /* ==================================================================
     二、按 kind 的节点渲染器
     ================================================================== */

  /* [消息底座收尾 2026-10-14 · 件2] 老卡环节块降级显示的判定集。
     迁移来的老卡环节块 `data` 只有 `{content, behavior}`，缺 verdict/steps/
     criteria_met/result 等结构化字段——**不解析历史自由文本去补字段**，
     直接降级成一条「历史记录（旧格式）」＋ content 原文。
     活跃事件这四类从不带 content，故「data.content 为非空串」＝老格式。 */
  const LEGACY_CONTENT_TYPES = { judgment: 1, plan_created: 1, step_advanced: 1, synthesize: 1 };

  function legacyRow(node) {
    const d = node.data || {};
    const row = makeRow(node.type,
      "历史记录（旧格式）" + fld("behavior", d.behavior),
      null, d.content, "void");
    const det = row.querySelector(".step-detail");
    if (det) det.style.display = "block";   // 原文默认展开可见（降级＝照示原文）
    return row;
  }

  /** chain：judgment / plan_created / step_start / step_advanced。
      计划步骤清单是同节点的附属块，故返回数组。 */
  function renderChain(node) {
    const d = node.data || {};
    if (LEGACY_CONTENT_TYPES[node.type] && typeof d.content === "string" && d.content) {
      return [legacyRow(node)];
    }
    const out = [];
    switch (node.type) {
      case "judgment":
        out.push(makeRow(node.type,
          "判断：" + (d.verdict === "direct" ? "直答" : "拆解上板") + fld("verdict", d.verdict)));
        break;
      case "plan_created": {
        out.push(makeRow(node.type,
          "计划上板：<b>" + esc(d.name || "计划") + "</b>" + fld("total", d.total) + fld("prompt", d.prompt),
          (d.total || 0) + " 步", fmtData(d)));
        const steps = Array.isArray(d.steps) ? d.steps : [];
        if (steps.length) out.push(makePlanSteps(steps));
        break;
      }
      case "step_start":
        out.push(makeSubRow(node.type,
          "步 " + esc(d.index || "") + "/" + esc(d.total || "") +
          ' <span class="bt">' + esc(btLabel(d.type)) + "</span> · " +
          esc(clip(d.prompt, 70)) + fld("step_id", d.step_id) + fld("criteria", d.criteria),
          null, fmtData(d), null, "lv2"));
        break;
      case "step_advanced": {
        const done = d.all_done;
        out.push(makeSubRow(node.type,
          (done ? "全部步骤完成，计划收口" : "步 " + esc(d.step_id || "") + " 完成") +
          fld("criteria_met", d.criteria_met) + fld("all_done", d.all_done) +
          fld("result", d.result) + fld("next_prompt", d.next_prompt),
          '<span class="mk ok">✓</span>' + (done ? "收口" : "→ 下一步"),
          fmtData(d), done ? "ok" : null, "lv2"));
        break;
      }
      default:
        out.push(sysRow(node));   // 未识别的 chain 类型兜底
    }
    return out;
  }

  /** sys：run_start / context / round_start / deliver / proposal / done / error /
      rescue / synthesize / card_message / triage_verdict / card_op / round_end / ping /
      未识别类型（C5 兜底）。 */
  function sysRow(node) {
    const d = node.data || {};
    const t = node.type;
    if (LEGACY_CONTENT_TYPES[t] && typeof d.content === "string" && d.content) return legacyRow(node);
    switch (t) {
      case "run_start":
        return makeRow(t, "一轮开始" + fld("pair_id", d.pair_id) + fld("chars", d.chars));
      case "context":
        return makeRow(t,
          "装配上下文" + fld("regions", (d.regions || []).join("、")) + fld("bytes", d.bytes),
          (d.regions || []).length + " 区 · " + Math.round((d.bytes || 0) / 1024) + "KB");
      case "round_start":
        return makeRow(t, "第 " + esc(d.round || "") + " 轮" + fld("phase", d.phase), null, null, "lv3");
      case "rescue":
        return makeRow(t, "文本抢救：截取可用片段续跑" + fld("count", d.count),
          esc(d.count || 0) + " 字符", fmtData(d));
      case "deliver":
        return makeRow(t, "交付拼接" + fld("branch", d.branch) + fld("bytes", d.bytes),
          Math.round((d.bytes || 0) / 1024) + "KB");
      case "proposal":
        if (!(d.count > 0)) return null;
        return makeRow(t, "建卡 " + esc(d.count) + " 张" + fld("cards", (d.cards || []).join("、")),
          esc(d.count) + " 卡", fmtData(d));
      case "card_message":
        return makeRow(t,
          "卡间消息：<b>" + esc(CARD_MSG_DISPLAY[d.type] || d.type || "") + "</b>" +
          fld("msg_id", d.msg_id) + fld("from", d.from),
          esc(d.type || ""), fmtData(d));
      case "triage_verdict":
        return makeRow(t, "旧判断事件（已退役）" + fld("verdict", d.verdict), "不会出现", fmtData(d), "void");
      case "done":
        return makeRow(t, "完成" + fld("rounds", d.rounds) + fld("model_calls", d.model_calls),
          esc(d.rounds || 0) + " 轮 · " + esc(d.model_calls || 0) + " 调用", null, "ok");
      case "error":
        return makeRow(t, "失败" + fld("error", d.error) + fld("rounds", d.rounds),
          '<span class="mk no">✗</span>失败', fmtData(d), "fail err");
      case "ping":
        // SSE 保活帧：注册表认它（不加会掉进 C5 兜底刷一屏「未识别事件」），
        // 但它不是给用户看的内容——照旧 renderEvent 的 default:break，不出行。
        return null;
      case "reasoning":
        // 该轮有思考却始终没有正式回复（run 以 error 收尾、或被后续轮顶替）：
        // 旧实现在这里直接丢弃思考文本，此处照示出来，不丢。
        return makeRow("reasoning", "思考（该轮无正式回复）", null, d.text || "", "void");
      default:
        // C5 兜底：注册表没有的类型照示出来，不丢不炸。
        return makeRow(t, "未识别事件" + fld("data", clip(JSON.stringify(d || {}), 60)),
          esc(t), fmtData(d), "void");
    }
  }

  function renderSys(node) {
    const el = sysRow(node);
    return el ? [el] : [];
  }

  /** injection：上下文注入四类（行样式由 type 标识，绝不渲染成聊天气泡）。 */
  function renderInjection(node) {
    const d = node.data || {};
    switch (node.type) {
      case "context_preamble":
        return [makeRow(node.type,
          "上下文注入·固定总领" + fld("source_kind", d.source_kind) +
          fld("source_ref", typeof d.source_ref === "string" ? d.source_ref : ""),
          esc(d.source_kind || ""), fmtData(d), "lv3")];
      case "context_injection": {
        const ref = d.source_ref || {};
        const refTxt = (typeof ref === "string")
          ? ref
          : [ref.script_name || ref.plan_name || "", ref.step_id || ""].filter(Boolean).join(" · ");
        return [makeRow(node.type,
          "上下文注入·当前步骤 " + esc(refTxt) +
          fld("source_kind", d.source_kind) + fld("step_id", d.step_id) +
          fld("index", d.index) + fld("total", d.total),
          esc(d.source_kind || ""), fmtData(d), "lv3")];
      }
      case "judgment_protocol":
        return [makeRow(node.type, "判断协议（轮首注入）" + fld("source_kind", d.source_kind),
          esc(d.source_kind || ""), fmtData(d), "lv3")];
      case "context_seat_identity":
        return [makeRow(node.type, "三席身份注入" + fld("source_kind", d.source_kind),
          esc(d.source_kind || ""), fmtData(d), "lv3")];
      default:
        return renderSys(node);
    }
  }

  /** tool：call 与 result 已在装配器合成一条节点（两态终态呈现）。
      live 命名空间的未闭合节点带 t0（帧到达时刻）——组重画时据此显示已用秒数，
      取代旧实现挂在那行上的 setInterval（行已被整组重画重建，定时器留不住）。 */
  function renderTool(node) {
    const call = node.call || {};
    const res = node.result || {};
    const hasResult = !!node.result;
    const ok = !!res.ok;
    const dur = node.duration ? (node.duration / 1000).toFixed(1) + "s" : "";
    if (!hasResult) {
      // 未收到结果（run 中断/结果帧未落盘）：仍出「调用中」行，信息不丢。
      const el = node.t0
        ? (performance.now() - node.t0) / 1000
        : 0;
      return [makeRow(node.type,
        "<b>" + esc(call.tool || "") + "</b>" + fld("target", call.target) + fld("step_id", call.step_id),
        '<span class="mk run"><i class="dot"></i>调用中' + (el ? " " + el.toFixed(1) + "s" : "") + "</span>",
        fmtData(call), "running")];
    }
    return [makeRow("tool_result",
      "<b>" + esc(res.tool || call.tool || "") + "</b> 返回" +
      fld("ok", res.ok) + fld("step_id", res.step_id) + fld("target", res.target),
      '<span class="mk ' + (ok ? "ok" : "no") + '">' + (ok ? "✓" : "✗") + "</span>" + dur +
      (res.step_id ? " #" + esc(res.step_id) : ""),
      fmtData(res), ok ? "ok" : "fail")];
  }

  /** seats：三席进场行（若有）＋ 席卡容器。 */
  function renderSeats(node) {
    const out = [];
    if (node.entered) {
      const d = node.enter || node.data || {};
      out.push(makeRow("seats_enter",
        "三席进场" + fld("tool_results", d.tool_results) + fld("steps", d.steps),
        "并行质询", null, "lv3"));
    }
    const members = node.members || {};
    if (Object.keys(members).length) out.push(makeSeats(members));
    return out;
  }

  /* ── 消息类渲染器（默认兜底；workspace 注入富版本）─────────────────
     stream.js 自带最小实现：① 本文件可独立跑（jsdom 取证脚本不依赖
     workspace 的整棵 DOM）；② workspace 注入后由它接管（内容格式化归它）。 */
  function fallbackMsgRenderer(kind) {
    return (node) => {
      const d = node.data || {};
      const el = document.createElement("div");
      el.className = "msg " + (kind === "user" ? "user" : (kind === "cardmsg" ? "cmsg" : "assistant"));
      el.dataset.seq = node.seq;
      const text = d.content == null ? "" : String(d.content);
      if (kind === "assistant") el.innerHTML = '<div class="md">' + renderMarkdown(text) + "</div>";
      else el.textContent = text;
      return el;
    };
  }

  const FALLBACK_MSG_RENDERERS = {
    user: fallbackMsgRenderer("user"),
    assistant: fallbackMsgRenderer("assistant"),
    cardmsg: fallbackMsgRenderer("cardmsg"),
  };
  let _msgRenderers = FALLBACK_MSG_RENDERERS;

  /** workspace 注入富消息渲染器（md 渲染 / 卡间信头剥离 / 元信息行）。 */
  function setMessageRenderers(m) {
    if (m && m.user && m.assistant && m.cardmsg) _msgRenderers = m;
  }

  function renderUser(node) { return _msgRenderers.user(node); }
  function renderAssistant(node) {
    const el = _msgRenderers.assistant(node);
    // 思考块插在正文之前（位置＝数据的函数：思考属本轮 assistant）
    if (node.reasoning) {
      const md = el.querySelector(".md");
      const think = makeThink(node.reasoning, { done: true, collapsed: true });
      if (md) el.insertBefore(think, md); else el.appendChild(think);
    }
    return el;
  }
  function renderCardMsg(node) { return _msgRenderers.cardmsg(node); }

  /** kind → 渲染器；返回元素数组（一个节点可出多块，如 plan_created）。 */
  const KIND_RENDER = {
    user: renderUser,
    assistant: renderAssistant,
    cardmsg: renderCardMsg,
    tool: renderTool,
    chain: renderChain,
    seats: renderSeats,
    sys: renderSys,
    injection: renderInjection,
  };

  function renderNode(node) {
    const fn = KIND_RENDER[node.kind] || renderSys;
    const out = fn(node) || [];
    const els = Array.isArray(out) ? out : [out];
    for (const el of els) {
      if (!el) continue;
      if (el.dataset && el.dataset.seq === undefined) el.dataset.seq = node.seq;
    }
    return els.filter(Boolean);
  }

  /* ==================================================================
     三、快照渲染：分组 → 三道（提问道 / 过程道 / 回复道）→ 折叠开关
     —— 位置全部来自节点数据，没有任何一处读 DOM 现状做推算。
     ================================================================== */

  /** [C6 过程折叠] 组内过程行的收起/展开开关。
      **默认展开**：沿用既有行为（stream.js 旧 addToggle 注释引 T2 用户裁定
      「折叠仅是用户可选减负、非默认隐藏」）；任务书括注写「done 后默认
      收起」，与该裁定相反，取代码里的既有用户裁定为准（见施工回报偏差②）。

      折叠态记在 st.fold（按组键），**不随重画丢失**——实时期每来一帧就整组重画，
      开关若只活在 DOM 里，用户刚收起就被下一帧弹开。 */
  function addGroupToggle(st, key, n, processEls) {
    const t = document.createElement("div");
    t.className = "step-toggle";
    let open = !st.fold.get(key);
    const paint = () => { t.textContent = (open ? "▴ 收起过程（" : "▾ 展开过程（") + n + " 步）"; };
    const apply = () => { for (const el of processEls) el.style.display = open ? "" : "none"; paint(); };
    paint();
    for (const el of processEls) el.style.display = open ? "" : "none";
    t.onclick = () => {
      open = !open;
      st.fold.set(key, !open);
      apply();
      scrollBottom();
    };
    return t;
  }

  /** 该轮是否已收口（与装配器判定「思考不再有归属」同口径）：
      组内已见 done/error，或本组之后又来了新轮。未收口的不给折叠开关——
      旧实现也只在 done/error 后加开关（见 renderEvent 的 done/error 分支）。 */
  function groupFinished(st, group, snap) {
    const terminal = group.nodes.some((n) => n.type === "done" || n.type === "error");
    if (terminal) return true;
    const keys = snap.rounds.map((g) => g.key);
    return keys.indexOf(group.key) < keys.length - 1;
  }

  /** 单组渲染：把 group 的节点铺进组容器（清空重画，不改容器本身）。 */
  function renderGroup(st, container, group, snap) {
    while (container.firstChild) container.removeChild(container.firstChild);
    const lanes = { user: [], process: [], reply: [] };
    for (const node of group.nodes) {
      const els = renderNode(node);
      if (!els.length) continue;
      // 次序铁律：本轮过程行在本轮正式回复之前 —— 提问道 → 过程道 → 回复道
      if (node.kind === "user") lanes.user.push(...els);
      else if (node.kind === "assistant" || node.kind === "cardmsg") lanes.reply.push(...els);
      else lanes.process.push(...els);
    }
    const pushLane = (els) => { for (const el of els) container.appendChild(el); };
    pushLane(lanes.user);
    pushLane(lanes.process);
    if (lanes.process.length && groupFinished(st, group, snap)) {
      container.appendChild(addGroupToggle(st, group.key, lanes.process.length, lanes.process));
    }
    pushLane(lanes.reply);
    container.dataset.round = group.round == null ? "" : String(group.round);
    container.dataset.key = group.key || "";
    return container;
  }

  function makeGroupContainer(st, group, snap) {
    const el = document.createElement("div");
    el.className = "msgs-round";
    return renderGroup(st, el, group, snap);
  }

  /** 全量渲染（刷新/换卡/结算 resync）：清空整列后按快照逐组铺。 */
  function renderSnapshot(st, snapshot) {
    const box = msgsEl();
    if (!box) return;
    while (box.firstChild) box.removeChild(box.firstChild);
    st.groups = new Map();
    st.liveSlot = null;
    for (const group of snapshot.rounds) {
      const el = makeGroupContainer(st, group, snapshot);
      box.appendChild(el);
      st.groups.set(group.key, el);
    }
    logInfo("[flow-render] snapshot(" + snapshot.rounds.length + " rounds)");
    scrollBottom();
  }

  /** 实时增量：只重画受影响的那一个组（其余组不碰）。 */
  function renderAffected(st, key) {
    const box = msgsEl();
    if (!box) return;
    const snap = st.asm.snapshot();
    const group = snap.rounds.find((g) => g.key === key);
    if (!group) return;
    let el = st.groups.get(key);
    if (!el) {
      el = makeGroupContainer(st, group, snap);
      const slot = st.liveSlot;
      if (slot && slot.parentNode === box) box.insertBefore(el, slot);
      else box.appendChild(el);
      st.groups.set(key, el);
    } else {
      renderGroup(st, el, group, snap);
    }
    logInfo("[flow-render] group-repaint(" + key + ")");
    scrollBottom();
  }

  /* ==================================================================
     四、装配入口
     ================================================================== */

  /** SSE 帧 → 落盘记录形态（装配器只认一种记录）。
      内存 journal 帧带 pair_id/turn 两个锚（event_stream._attach_anchors），
      落盘七键叫 run_id/round——**这里只做形翻译，不做语义加工**；
      另打 ns="live"：落盘 seq 与 journal seq 是两套计数空间，去重必须隔离。 */
  function frameToRecord(frame) {
    return {
      ns: "live",
      seq: frame && frame.seq,
      run_id: String((frame && frame.pair_id) || ""),
      round: Number((frame && frame.turn) || 0) || 0,
      type: (frame && frame.type) || "",
      data: (frame && frame.data) || {},
      model: (frame && frame.model) || "",
    };
  }

  function ensureRun(wid, cid) {
    if (current && current.wid === wid && current.cid === cid) return current;
    stop();
    current = {
      wid, cid, es: null, esT: null,
      asm: createAssembler(), groups: new Map(), liveSlot: null, liveRunId: "", settled: false,
      fold: new Map(),          // 组键 → 折叠态（true＝已收起）：重画不丢用户的选择
      textEl: null, textBody: null, reasoning: null, reasoningText: null,
      fallbackUsed: false, lastSeq: 0, lastTokenSeq: 0,
      textRaw: "", _mdTimer: null,
    };
    return current;
  }

  /** 实时帧入装配：applyRecord → 只重画受影响组。
      装配器**全程同一个实例**（不是每轮换一个）：重建走 rebuild()（清空后重灌），
      增量走 applyRecord()；两者共用一套去重与到达序。
      结算（resync）后置 settled：此后到达的实时帧一律不收——否则重建后的
      落盘记录与迟到帧会在同一组里长出重复行（实测已踩，见 pullUserBubble 注）。 */
  function applyLiveFrame(st, frame) {
    if (st.settled) return;
    const rec = frameToRecord(frame);
    if (!rec.type) return;
    if (rec.run_id) st.liveRunId = rec.run_id;
    const node = st.asm.applyRecord(rec);
    if (node && node.kind === "tool" && !node.result && !node.t0) node.t0 = performance.now();
    const key = (node && node.groupKey) || groupKeyOf(rec);
    renderAffected(st, key);
  }

  /** 刷新/换卡恢复：GET /timeline → rebuild → renderSnapshot。
      ★ 断 timeline 即会话区空——单源单通道的代价与证据（判据2）：
      这里**不做任何第二数据源兜底**，也不保留上一轮的 DOM。 */
  async function replayTrace(wid, cid) {
    const st = ensureRun(wid, cid);
    stopES();
    releaseText(st);          // 交出 live 正文引用：它已由快照里的正式节点接管
    const items = await fetchTimeline(st, wid, cid);
    renderSnapshot(st, st.asm.rebuild(items));
  }

  /** 结算后回流：与刷新同一条路（唯一数据源仍是 timeline）。
      POST 返回＝本轮事件已全部落盘，此刻拉 timeline 必然含 assistant_text。
      返回 {ok, done}：done＝最后一轮已出现 assistant 节点或 done/error 收口
      （workspace 的断线自愈轮询据此判「已结算」，不再读 /session 数条数）。 */
  async function resync(wid, cid) {
    const st = ensureRun(wid, cid);
    const items = await fetchTimeline(st, wid, cid);
    st.settled = true;           // 收口：此后到达的实时帧不再入装配（见 applyLiveFrame 注）
    releaseText(st);             // live 气泡随整列重画消失，由正式节点接管
    const snap = st.asm.rebuild(items);
    renderSnapshot(st, snap);
    const last = snap.rounds[snap.rounds.length - 1];
    const done = !!(last && last.nodes.some((n) =>
      n.kind === "assistant" || n.type === "done" || n.type === "error"));
    return { ok: items !== null, done, runId: st.liveRunId };
  }

  async function fetchTimeline(st, wid, cid) {
    try {
      const resp = await API.getTimeline(wid, cid);
      return (resp && resp.items) || [];
    } catch (e) {
      return null;     // 拉不到＝没有数据源，如实空屏（不兜底、不静默保留旧 DOM）
    }
  }

  /** 发送后尽早把本轮 user 气泡从 timeline 取回（唯一数据源，不是命令式上屏）。
      POST 要跑到本轮结束才返回，故不等它：立刻拉一次，没取到就短间隔重试。

      ★ 只补「提问」这一类记录，**不整表重建**——两个理由，都是实测踩出来的：
        ① 整表 rebuild 会把并行的实时帧（ns="live"）一起抹掉，本轮过程行闪没；
        ② 同一条过程事件在落盘与 SSE 里各有一份（两套 seq 空间），重建后
           实时帧再进来就成两条重复行。故此处只 applyRecord 缺失的
           user_message（它从不走 SSE，不存在重复面），其余一律留给结算 resync。 */
  async function pullUserBubble(wid, cid) {
    const st = ensureRun(wid, cid);
    for (let i = 0; i < 6; i++) {
      const items = await fetchTimeline(st, wid, cid);
      if (items === null) return false;
      for (const rec of items) {
        if (!rec || rec.type !== "user_message") continue;
        const key = groupKeyOf(rec);
        const g = st.asm.snapshot().rounds.find((x) => x.key === key);
        if (g && g.nodes.some((n) => n.kind === "user")) continue;   // 已有，不重复入
        st.asm.applyRecord(Object.assign({ ns: "disk" }, rec));
        renderAffected(st, key);
      }
      const last = st.asm.snapshot().rounds.slice(-1)[0];
      if (last && last.nodes.some((n) => n.kind === "user")) return true;
      await new Promise((r) => setTimeout(r, 500));
    }
    return false;
  }

  /* ==================================================================
     五、逐字正文（live 气泡）：/tokens 直写，不进装配器
     ================================================================== */

  function liveSlot(st) {
    const box = msgsEl();
    if (!box) return null;
    if (!st.liveSlot || !st.liveSlot.isConnected) {
      const slot = document.createElement("div");
      slot.className = "live-slot";
      box.appendChild(slot);
      st.liveSlot = slot;
    }
    return st.liveSlot;
  }

  /** 正文条目恒压队尾（live 槽恒在轮组之后）。 */
  function keepTextLast(st) {
    const slot = liveSlot(st);
    if (!slot || !st.textEl) return;
    if (st.textEl.parentNode !== slot) slot.appendChild(st.textEl);
  }

  function textEntry(st) {
    if (st.textEl) return st;
    const slot = liveSlot(st);
    if (!slot) return st;
    const el = document.createElement("div");
    el.className = "msg assistant live";
    el.innerHTML = '<div class="md live-content"></div>';
    slot.appendChild(el);
    st.textEl = el;
    st.textBody = el.querySelector(".live-content");
    logInfo("[flow-render] msg created(assistant.live)");
    scrollBottom();
    return st;
  }

  /* [⑧ 边出边渲染 2026-10-14] 流式正文节流渲染：最多每 100ms 渲染一次。 */
  function scheduleMd(st) {
    if (st._mdTimer) return;
    st._mdTimer = setTimeout(() => {
      st._mdTimer = null;
      if (!st.textBody) return;
      st.textBody.innerHTML = renderMarkdown(st.textRaw);
      keepTextLast(st);
      scrollBottom();
    }, 100);
  }

  function flushMd(st) {
    if (st._mdTimer) { clearTimeout(st._mdTimer); st._mdTimer = null; }
    if (st.textBody && st.textRaw) {
      st.textBody.innerHTML = renderMarkdown(st.textRaw);
      keepTextLast(st);
    }
  }

  /** 主模型 think（reasoning_chunk 不带 model）：live 可折叠思考块。 */
  function ensureReasoning(st) {
    if (st.reasoning) return st;
    textEntry(st);
    if (!st.textEl) return st;
    const r = document.createElement("div");
    r.className = "think thinking";
    r.innerHTML =
      '<div class="think-head"><i class="dot"></i>' +
      '<span class="think-lab">思考中</span>' +
      '<span class="think-time">0.0s</span>' +
      '<span class="think-caret">▾</span></div>' +
      '<div class="think-body"></div>';
    st.textEl.insertBefore(r, st.textBody);
    st.reasoning = r;
    st.reasoningText = r.querySelector(".think-body");
    st._thinkT0 = performance.now();
    const timeEl = r.querySelector(".think-time");
    st._thinkTimer = setInterval(() => {
      if (!st._thinkT0) return;
      timeEl.textContent = ((performance.now() - st._thinkT0) / 1000).toFixed(1) + "s";
    }, 200);
    r.querySelector(".think-head").onclick = () => {
      const open = r.classList.toggle("collapsed");
      r.querySelector(".think-caret").textContent = open ? "▸" : "▾";
      scrollBottom();
    };
    return st;
  }

  /** think 收尾：停走秒、头行转「已思考 Ns」。 */
  function finishReasoning(st) {
    if (!st.reasoning) return;
    if (st._thinkTimer) { clearInterval(st._thinkTimer); st._thinkTimer = null; }
    if (st._thinkT0) {
      const s = ((performance.now() - st._thinkT0) / 1000).toFixed(1);
      st._thinkT0 = 0;
      const lab = st.reasoning.querySelector(".think-lab");
      const t = st.reasoning.querySelector(".think-time");
      if (lab) lab.textContent = "已思考";
      if (t) t.textContent = s + "s";
      st.reasoning.classList.remove("thinking");
    }
  }

  function appendChunk(st, kind, text, seq, model) {
    if (!text) return;
    if (kind === "reasoning") {
      // 带 model 的是三席思考（并发交织）→ 实时落进该轮 seats 节点（装配器内合流）；
      // 不带 model 的是主模型思考 → 仍落 live 正文条目内的弱化区。
      if (model) {
        applyLiveFrame(st, {
          seq: seq, type: "reasoning", pair_id: st.liveRunId,
          turn: 0, data: { text: text, model: model },
        });
        return;
      }
      ensureReasoning(st).reasoningText.textContent += text;
    } else {
      textEntry(st);
      if (!st.textBody) return;
      st.textRaw += text;
      scheduleMd(st);
      keepTextLast(st);
    }
    logInfo("[flow-render] chunk(" + kind + ", " + text.length + ", " + (seq == null ? "-" : seq) + ")");
    scrollBottom();
  }

  function releaseText(st) {
    st.textEl = null; st.textBody = null; st.reasoning = null; st.reasoningText = null;
    st.textRaw = "";
    if (st._mdTimer) { clearTimeout(st._mdTimer); st._mdTimer = null; }
    if (st._thinkTimer) { clearInterval(st._thinkTimer); st._thinkTimer = null; }
    st._thinkT0 = 0;
  }

  /** 轮界：正文条目只留当前这一轮（切轮时清字，不动任何已渲染的组）。 */
  function clearText(st) {
    if (!st.textEl) return;
    if (st.textBody) st.textBody.innerHTML = "";
    st.textRaw = "";
    if (st._mdTimer) { clearTimeout(st._mdTimer); st._mdTimer = null; }
    if (st.reasoningText) st.reasoningText.textContent = "";
    if (st.reasoning && !(st.reasoningText && st.reasoningText.textContent)) {
      try { st.reasoning.remove(); } catch (e) { /* noop */ }
      st.reasoning = null; st.reasoningText = null;
    }
    keepTextLast(st);
  }

  function onTokenFrame(st, evt) {
    if (evt && typeof evt.seq === "number" && evt.seq > st.lastTokenSeq) st.lastTokenSeq = evt.seq;
    const type = (evt && evt.type) || "";
    const data = (evt && evt.data) || {};
    const text = data.text || "";
    const model = data.model || "";
    // 轮界标记帧（type 复用 run_start/round_start）
    if (type === "run_start" || type === "round_start") { clearText(st); return; }
    if (type === "content_chunk") appendChunk(st, "content", text, evt && evt.seq, model);
    else if (type === "reasoning_chunk") appendChunk(st, "reasoning", text, evt && evt.seq, model);
  }

  /* ==================================================================
     六、流启停
     ================================================================== */

  function finish() {
    finishReasoning(current || {});
    flushMd(current || {});
    scrollBottom();
  }

  function stopES() {
    if (current && current.es) { try { current.es.close(); } catch (e) { /* noop */ } current.es = null; }
    if (current && current.esT) { try { current.esT.close(); } catch (e) { /* noop */ } current.esT = null; }
  }

  function stop() {
    stopES();
    current = null;
  }

  /** 开始跟踪新一轮：两路 SSE。
      过程帧（/stream）→ 装配器 → 受影响轮组重画；
      逐字帧（/tokens）→ live 正文气泡（不进装配器）。
      onFail 在事件流连不上时回调一次（降级保底）。 */
  function start(wid, cid, onFail) {
    const st = ensureRun(wid, cid);
    stopES();
    st.liveRunId = "";        // 新一轮：轮锚随 run_start 帧到达后才落
    st.settled = false;       // 解冻：上一轮的收口状态不带到这一轮
    try {
      // 无游标（lastSeq=0）＝从当前末帧起；有游标＝断点续传。
      const sFrom = st.lastSeq > 0 ? st.lastSeq + 1 : 0;
      const es = new EventSource(`/api/works/${wid}/cards/${cid}/stream?from=${sFrom}`);
      st.es = es;
      es.onmessage = (m) => {
        try {
          const frame = JSON.parse(m.data);
          if (typeof frame.seq === "number" && frame.seq > st.lastSeq) st.lastSeq = frame.seq;
          if (frame.type === "done" || frame.type === "error") finish();
          applyLiveFrame(st, frame);
        } catch (e) { /* 单帧坏不致命 */ }
      };
      es.onerror = () => {
        if (!st.fallbackUsed) {
          st.fallbackUsed = true;
          if (typeof onFail === "function") onFail();
        }
      };
    } catch (e) {
      if (!st.fallbackUsed && typeof onFail === "function") { st.fallbackUsed = true; onFail(); }
    }

    // 逐字流独立 SSE。刻意**不**接 onFail：连不上只少逐字显示。
    try {
      const tFrom = st.lastTokenSeq > 0 ? st.lastTokenSeq + 1 : 0;
      const esT = new EventSource(`/api/works/${wid}/cards/${cid}/tokens?from=${tFrom}`);
      st.esT = esT;
      esT.onmessage = (m) => {
        try { onTokenFrame(st, JSON.parse(m.data)); } catch (e) { /* 单帧坏不致命 */ }
      };
      esT.onerror = () => { /* 静默 */ };
    } catch (e) { /* 同上 */ }
    return st;
  }

export const EventFlow = {
  start, stop, replayTrace, resync, pullUserBubble, setMessageRenderers,
};
