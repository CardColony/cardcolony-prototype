/* stream.js - [会话渲染单源化 2026-10-15] 单一事件流 → 装配器 → 节点快照 → 快照渲染。
 *            [对话流照抄 dsh 2026-10-16] 渲染层改为**按节点增量装配**（dsh A1/A2）。
 *
 *   用户裁定（任务书《会话渲染单源化》C1–C6 ＋《对话流照抄 dsh》T1/T2/T3/T4）：
 *     C1 单源单通道：timeline（落盘事件流）是**唯一**渲染数据源；/session 的
 *        消息渲染通道（workspace.renderChat）已退役，本文件不读它。
 *     C2 位置由数据自带：节点位置＝(分组键, 到达序)，渲染**只读快照**。
 *        旧缝合族的游标计数、插入锚点、思考归位回扫、消息跳过表、组封存
 *        五件套已**整体删除**（不是注释掉）；本文件不再有任何一处读 DOM
 *        现状做位置推算（红线6 的 grep 判据＝0）。
 *     C4 缝合族删净，不是注释掉。
 *     C5 未知事件类型不丢：落 sys 兜底行「未识别事件」。
 *     C6 过程折叠：组容器带收起/展开开关（默认展开，理由见 paintToggle）。
 *     T2 增量装配：每个节点带稳定 key ＋ 内容版本 rev；渲染层维护
 *        「组键 → 组视图 → 节点键 → 已渲染元素」，只重建 rev 变了的节点，
 *        既有元素**原地复用**。结构未变时快照返回同一引用，视图层零重排。
 *        **不存在「收到新东西就整列重建」的路径**——实时帧只 upsert 它自己那一组。
 *
 *   ── 数据流（刷新与实时同一条）──
 *     刷新/换卡：GET /timeline → assembler.rebuild(items) → snapshot() → renderSnapshot
 *     实时：    /stream 帧 → frameToRecord() → assembler.applyRecord() →
 *               **只 upsert 受影响的那一个轮组**（renderAffected → renderGroup）
 *     结算：    POST 返回（此时事件已全部落盘）→ resync() 走回第一条路
 *     乐观上屏：send() 瞬间 → optimisticUser() → 本地轮（ns="local"）→
 *               真实 run 记录到达时装配器「认领」该轮（改名 run:<id>），
 *               真实 user_message 到达时**原地升级**，不产生双气泡。
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
import { API } from "./esm/api.js";
import { escapeHtml as esc } from "./esm/util.js";
/* [边出边渲染 2026-10-14 · 用户令 ⑧] 流式期间也要渲染 md */
import { renderMarkdown } from "./esm/md.js";
import { createAssembler, groupKeyOf } from "./esm/assembler.js";

let current = null;   // {wid,cid,es,esT,asm,groups,liveSlot,liveRunId,...}

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

  /* ── Think 行（dsh ReasoningRow 规格）──────────────────────────────
     折叠单行＝图标 ＋ 标题「Think」 ＋ 分隔点 ＋ **摘要** ＋ 计时。
     摘要：运行中取**最新一行**（跟着思考走），完成取**第一行**（一眼知主题）。
     点击展开全文（灰斜体，22px 缩进）。
     dsh 依据：ui-conversation ReasoningRow.tsx / ReasoningRow.module.css。 */
  function thinkSummary(text, running) {
    const lines = String(text == null ? "" : text).split("\n").map(s => s.trim()).filter(Boolean);
    if (!lines.length) return "";
    return running ? lines[lines.length - 1] : lines[0];
  }

  function makeThink(text, opts) {
    const o = opts || {};
    const r = document.createElement("div");
    r.className = "think" + (o.done ? " done" : " thinking") + " collapsed";
    r.innerHTML =
      '<div class="think-head">' +
        '<i class="think-ico"></i>' +
        '<span class="think-lab">Think</span>' +
        '<span class="think-sep">·</span>' +
        '<span class="think-sum"></span>' +
        '<span class="think-time"></span>' +
      "</div>" +
      '<div class="think-body"></div>';
    r.querySelector(".think-sum").textContent = thinkSummary(text, !o.done);
    r.querySelector(".think-body").textContent = text || "";
    r.querySelector(".think-head").onclick = () => {
      r.classList.toggle("collapsed");
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

  /** 把工具结果对象摊成可读内容块（展开体里逐块渲染）。
      result 典型形态（真实数据实读）：
        {ok:false, error:{code,message}}   → 红色错误块
        {ok:true, content:"..."}           → 内容段（正文）
        {ok:true, files:[...]}             → 文件列表
        其余字段                           → 键值对逐行
      escape-first：所有文本经 esc()，绝不 innerHTML 塞未转义内容。 */
  function renderResultContent(d) {
    const out = [];
    if (!d || typeof d !== "object") {
      const p = document.createElement("pre");
      p.className = "tr-pre";
      p.textContent = d == null ? "" : String(d);
      out.push(p);
      return out;
    }
    const err = d.error;
    if (err) {
      const e = document.createElement("div");
      e.className = "tr-err";
      e.textContent = (err.code ? "[" + err.code + "] " : "") + (err.message || "调用失败");
      out.push(e);
    }
    if (d.content != null && d.content !== "") {
      const c = document.createElement("div");
      c.className = "tr-content";
      c.textContent = String(d.content).length > 4000
        ? String(d.content).slice(0, 4000) + "…（已截断）"
        : String(d.content);
      out.push(c);
    }
    if (Array.isArray(d.files) && d.files.length) {
      const ul = document.createElement("ul");
      ul.className = "tr-files";
      for (const f of d.files.slice(0, 200)) {
        const li = document.createElement("li");
        li.textContent = String(f);
        ul.appendChild(li);
      }
      out.push(ul);
    }
    if (Array.isArray(d.items) && d.items.length) {
      const ul = document.createElement("ul");
      ul.className = "tr-files";
      for (const it of d.items.slice(0, 200)) {
        const li = document.createElement("li");
        li.textContent = typeof it === "object" ? fmtData(it) : String(it);
        ul.appendChild(li);
      }
      out.push(ul);
    }
    // 其余键值对逐行（不含已提取的）
    const rest = {};
    for (const k of Object.keys(d)) {
      if (k === "ok" || k === "error" || k === "content" || k === "files" || k === "items") continue;
      if (typeof d[k] === "object") continue;
      if (d[k] === null || d[k] === "") continue;
      rest[k] = d[k];
    }
    const rks = Object.keys(rest);
    if (rks.length) {
      const kv = document.createElement("div");
      kv.className = "tr-kv";
      kv.innerHTML = rks.map(k =>
        "<span class='f'><span class='k'>" + esc(k) + "</span> = <span class='v'>" + esc(String(rest[k])) + "</span></span>"
      ).join(" ");
      out.push(kv);
    }
    return out;
  }

  /** 工具参数摘要（dsh ToolRow 的 FILL 截断摘要）。 */
  function argSummary(args) {
    if (!args || typeof args !== "object") return "";
    const parts = Object.keys(args).map(k => k + "=" + args[k]);
    return clip(parts.join(" "), 80);
  }

  /** 工具结果一句话摘要（折叠态可见；完整内容点开才看——dsh 两段式）。 */
  function resultSummary(res, hasResult, ok) {
    if (!hasResult) return "调用中…";
    const d = res || {};
    const err = d.error;
    if (err) return clip((err.code ? "[" + err.code + "] " : "") + (err.message || "调用失败"), 100);
    if (d.content != null && d.content !== "") {
      const first = String(d.content).split("\n").find(s => s.trim()) || "";
      return clip(first, 100);
    }
    if (Array.isArray(d.files) && d.files.length) return d.files.length + " 个文件";
    if (Array.isArray(d.items) && d.items.length) return d.items.length + " 条";
    const rk = Object.keys(d).filter(k => k !== "ok" && typeof d[k] !== "object" && d[k] !== "" && d[k] != null);
    return rk.length ? clip(rk.map(k => k + "=" + d[k]).join(" "), 100) : (ok ? "完成" : "失败");
  }

  /** tool：call 与 result 已在装配器合成一条节点（两态终态呈现）。
      dsh ToolRow 规格（ui-tool ToolRow.tsx / ToolRow.module.css）：
        恒一行 ＝ 前导槽（工具图标／运行中扫光／错误红点） ＋ 工具名 ＋
        参数摘要 ＋ 状态（运行中动画／✓ 耗时／✗ 错误）；下一行是结果首段摘要；
        点整行展开完整内容（IN/OUT 卡，max-height 内滚）。默认折叠。 */
  function renderTool(node) {
    const call = node.call || {};
    const res = node.result || {};
    const hasResult = !!node.result;
    const ok = !!res.ok;
    const toolName = res.tool || call.tool || "";
    const args = argSummary(call.arguments);
    const row = document.createElement("div");
    row.className = "step-row tool-row" + (hasResult ? (ok ? " ok" : " fail") : " running");
    row.dataset.ev = hasResult ? "tool_result" : "tool_call";
    row.innerHTML =
      '<span class="ev">' + esc(hasResult ? "tool_result" : "tool_call") + "</span>" +
      '<span class="txt"><i class="tool-ico"></i><b>' + esc(toolName) + "</b>" +
        (args ? '<span class="targs">' + esc(args) + "</span>" : "") + "</span>" +
      '<span class="sub"></span>';
    const sub = row.querySelector(".sub");
    if (!hasResult) {
      sub.innerHTML = '<span class="mk run">调用中</span>';
    } else {
      sub.innerHTML = '<span class="mk ' + (ok ? "ok" : "no") + '">' + (ok ? "✓" : "✗") + "</span>" +
        (node.duration ? esc((node.duration / 1000).toFixed(1) + "s") : "");
    }
    const sum = document.createElement("div");
    sum.className = "tool-sum";
    sum.textContent = resultSummary(res, hasResult, ok);
    row.appendChild(sum);
    // 完整内容：默认折叠（点整行展开）
    const body = document.createElement("div");
    body.className = "tool-body" + (ok ? "" : " err");
    for (const p of renderResultContent(res.result || res)) body.appendChild(p);
    row.appendChild(body);
    row.classList.add("has-detail");
    row.onclick = (e) => {
      if (e.target.closest(".tool-body")) return;
      row.classList.toggle("open");
    };
    return [row];
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
      el.className = "msg " + (kind === "user" ? "user" : (kind === "cardmsg" ? "cmsg" : "assistant")) +
        (node.pending ? " pending" : "");
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
      const think = makeThink(node.reasoning, { done: true });
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
     三、快照渲染：增量装配（dsh A1/A2）
     —— 位置全部来自节点数据，没有任何一处读 DOM 现状做推算。
        渲染层维护「组视图」：容器 ＋ 节点键 → 已渲染元素。
        节点 rev 未变 ⇒ 复用原元素（零 DOM 操作）；变了 ⇒ 原地替换那一个节点；
        新节点 ⇒ 追加。**既有元素永不因别处变化而被搬动**。
     ================================================================== */

  function groupView(el) {
    return { el, views: new Map(), toggle: null };
  }

  /** 取/建组视图：优先用内存视图；内存丢了就按 data-key **认领屏上容器**；
      都没有才新建。这样即便渲染状态被重建（stop / 换卡 / 重开同卡），
      也不会在列里长出重复的轮容器。 */
  function ensureGroupView(st, box, key) {
    let gv = st.groups.get(key);
    if (gv && gv.el && gv.el.parentNode === box) return gv;
    const el = box.querySelector(':scope > .msgs-round[data-key="' + key + '"]');
    if (el) {
      gv = groupView(el);
      st.groups.set(key, gv);
      return gv;
    }
    const neo = document.createElement("div");
    neo.className = "msgs-round";
    neo.dataset.key = key;
    gv = groupView(neo);
    st.groups.set(key, gv);
    const slot = st.liveSlot;
    if (slot && slot.parentNode === box) box.insertBefore(neo, slot);
    else box.appendChild(neo);
    return gv;
  }

  /** 三道分道（次序铁律：本轮过程行恒在本轮正式回复之前）。 */
  function lanesOf(group) {
    const user = [], process = [], reply = [];
    for (const n of group.nodes) {
      if (n.kind === "user") user.push(n);
      else if (n.kind === "assistant" || n.kind === "cardmsg") reply.push(n);
      else process.push(n);
    }
    return { user, process, reply };
  }

  /* [C6 过程折叠] 组内过程行的收起/展开开关。**默认展开**：沿用既有行为
     （stream.js 旧 addToggle 注释引 T2 用户裁定「折叠仅是用户可选减负、
     非默认隐藏」）。折叠态记在 st.fold（按组键），**不随重画丢失**。 */
  function ensureToggle(gv, st) {
    if (gv.toggle) return gv.toggle;
    const t = document.createElement("div");
    t.className = "step-toggle";
    t.onclick = () => {
      const k = t.__key;
      st.fold.set(k, !st.fold.get(k));
      paintToggle(t, st, k, t.__els || []);
      scrollBottom();
    };
    gv.toggle = t;
    return t;
  }

  function paintToggle(t, st, key, processEls) {
    t.__key = key;
    t.__els = processEls;
    const open = !st.fold.get(key);
    t.textContent = (open ? "▴ 收起过程（" : "▾ 展开过程（") + processEls.length + " 步）";
    for (const el of processEls) el.style.display = open ? "" : "none";
  }

  /** 单组增量渲染：只重建 rev 变了的节点，其余元素原地复用。 */
  function renderGroup(st, gv, group, snap) {
    const { user, process, reply } = lanesOf(group);

    // ① 逐节点取/建/更新（只碰内容变了的节点）
    const collect = (nodes) => {
      const out = [];
      for (const node of nodes) {
        const key = node.key;
        let v = gv.views.get(key);
        if (!v) {
          const els = renderNode(node);
          v = { els, rev: node.rev || 0 };
          gv.views.set(key, v);
        } else if (v.rev !== (node.rev || 0)) {
          const els = renderNode(node);
          const anchor = (v.els[0] && v.els[0].parentNode === gv.el) ? v.els[0] : null;
          for (const el of els) gv.el.insertBefore(el, anchor);
          for (const el of v.els) if (el.parentNode === gv.el) gv.el.removeChild(el);
          v.els = els;
          v.rev = node.rev || 0;
        }
        for (const el of v.els) out.push(el);
      }
      return out;
    };
    const userEls = collect(user);
    const processEls = collect(process);
    const replyEls = collect(reply);

    // ② 折叠开关：组已收口且有过程行才有（未收口不给开关——与旧实现同口径）
    let toggle = null;
    if (processEls.length && group.finalized) {
      toggle = ensureToggle(gv, st);
      paintToggle(toggle, st, group.key, processEls);
    } else if (gv.toggle) {
      if (gv.toggle.parentNode) gv.toggle.parentNode.removeChild(gv.toggle);
      gv.toggle = null;
    }

    // ③ 落位：按 desired 序校正（已就位者 insertBefore 为 no-op ⇒ 零搬动）
    const place = (el, prev) => {
      if (prev === null) {
        if (gv.el.firstChild !== el) gv.el.insertBefore(el, gv.el.firstChild);
      } else if (prev.nextSibling !== el) {
        gv.el.insertBefore(el, prev.nextSibling);
      }
    };
    let prev = null;
    for (const el of userEls) { place(el, prev); prev = el; }
    for (const el of processEls) { place(el, prev); prev = el; }
    if (toggle) { place(toggle, prev); prev = toggle; }
    for (const el of replyEls) { place(el, prev); prev = el; }

    // ④ 剪枝：本组已不存在的节点/元素移除（revoke 软删、组改名等）
    const keep = new Set(userEls.concat(processEls, replyEls));
    if (toggle) keep.add(toggle);
    for (const child of Array.from(gv.el.children)) {
      if (!keep.has(child)) gv.el.removeChild(child);
    }
    const liveKeys = new Set(group.nodes.map((n) => n.key));
    for (const k of Array.from(gv.views.keys())) {
      if (!liveKeys.has(k)) gv.views.delete(k);
    }

    gv.el.dataset.round = group.round == null ? "" : String(group.round);
    gv.el.dataset.key = group.key || "";
    return gv.el;
  }

  /** 快照里已不存在的组容器移除（本地轮认领改名后旧键即属此类）。 */
  function pruneGroups(st, snap) {
    const live = new Set(snap.rounds.map((g) => g.key));
    for (const [k, gv] of Array.from(st.groups)) {
      if (live.has(k)) continue;
      if (gv.el.parentNode) gv.el.parentNode.removeChild(gv.el);
      st.groups.delete(k);
    }
  }

  /** 全量快照：**复用**既有组容器与既有节点元素，只建缺的、只改变的。
      刷新/换卡/结算都走这里——历史轮组键不变 ⇒ DOM 一个都不动。 */
  function renderSnapshot(st, snapshot) {
    const box = msgsEl();
    if (!box) return;
    const want = new Set(snapshot.rounds.map((g) => g.key));
    // 剪枝：列里所有不在本次快照的轮容器移除（含换卡后遗留、状态重建后遗留）
    for (const el of Array.from(box.querySelectorAll(":scope > .msgs-round"))) {
      if (!want.has(el.dataset.key || "")) box.removeChild(el);
    }
    for (const k of Array.from(st.groups.keys())) if (!want.has(k)) st.groups.delete(k);
    for (const group of snapshot.rounds) {
      renderGroup(st, ensureGroupView(st, box, group.key), group, snapshot);
    }
    // 组序按快照轮序校正（历史组已就位 ⇒ no-op）
    let prev = null;
    for (const group of snapshot.rounds) {
      const gv = st.groups.get(group.key);
      const el = gv && gv.el;
      if (!el) continue;
      if (prev === null) {
        if (box.firstChild !== el) box.insertBefore(el, box.firstChild);
      } else if (prev.nextSibling !== el) {
        box.insertBefore(el, prev.nextSibling);
      }
      prev = el;
    }
    logInfo("[flow-render] snapshot(" + snapshot.rounds.length + " rounds)");
    scrollBottom();
  }

  /** 实时增量：只 upsert 受影响的那一组（其余组不碰）。 */
  function renderAffected(st, key) {
    const box = msgsEl();
    if (!box) return;
    const snap = st.asm.snapshot();
    const group = snap.rounds.find((g) => g.key === key);
    if (!group) return;
    renderGroup(st, ensureGroupView(st, box, key), group, snap);
    pruneGroups(st, snap);
    logInfo("[flow-render] group-upsert(" + key + ")");
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
      textEl: null, textBody: null, reasoning: null, reasoningText: null, reasoningSum: null,
      fallbackUsed: false, esDead: false, lastSeq: 0, lastTokenSeq: 0,
      textRaw: "", _mdTimer: null,
    };
    return current;
  }

  /** 实时帧入装配：applyRecord → 只 upsert 受影响组。
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
    const key = (node && node.groupKey) || groupKeyOf(rec);
    renderAffected(st, key);
  }

  /** 乐观上屏（T1）：点发送瞬间把用户气泡落进本地轮，不等 POST/timeline。 */
  let _localSeq = 0;
  function optimisticUser(wid, cid, content) {
    const st = ensureRun(wid, cid);
    const localKey = "u" + (++_localSeq);
    const node = st.asm.applyRecord({
      ns: "local", seq: 0, local_key: localKey, type: "user_message",
      run_id: "", round: 0, data: { content: String(content == null ? "" : content) }, model: "",
    });
    renderAffected(st, (node && node.groupKey) || ("local:" + localKey));
    return localKey;
  }

  /** 发送失败回退：撤下未被认领的本地轮（幽灵气泡不留在屏上）。 */
  function dropOptimisticUser(wid, cid) {
    const st = ensureRun(wid, cid);
    if (st.asm.dropLocal()) renderSnapshot(st, st.asm.snapshot());
  }

  /** 清掉 live 逐字槽（正式节点接管时调用）。 */
  function clearLiveSlot(st) {
    releaseText(st);
    if (st.liveSlot && st.liveSlot.parentNode) st.liveSlot.parentNode.removeChild(st.liveSlot);
    st.liveSlot = null;
  }

  /** 刷新/换卡恢复：GET /timeline → rebuild → renderSnapshot。
      ★ 断 timeline 即会话区空——单源单通道的代价与证据（判据2）：
      这里**不做任何第二数据源兜底**，也不保留上一轮的 DOM。 */
  async function replayTrace(wid, cid) {
    const st = ensureRun(wid, cid);
    stopES();
    clearLiveSlot(st);        // 交出 live 正文引用：它已由快照里的正式节点接管
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
    clearLiveSlot(st);           // live 气泡随正式节点接管退场
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

  /** 乐观上屏的对账（T1）：把 timeline 里已落盘的 user_message 灌进装配器——
      pending 节点按内容**原地升级**（不新增节点、不出双气泡），其余记录去重跳过。
      ★ 只补这一类记录，**不整表重建**：整表 rebuild 会抹掉并行的实时帧
        （ns="live"），本轮过程行会闪没。 */
  async function pullUserBubble(wid, cid) {
    const st = ensureRun(wid, cid);
    for (let i = 0; i < 6; i++) {
      const items = await fetchTimeline(st, wid, cid);
      if (items === null) return false;
      let confirmed = false;
      for (const rec of items) {
        if (!rec || rec.type !== "user_message") continue;
        const node = st.asm.applyRecord(Object.assign({ ns: "disk" }, rec));
        const key = (node && node.groupKey) || groupKeyOf(rec);
        renderAffected(st, key);
        if (node && node.kind === "user" && !node.pending) confirmed = true;
      }
      if (confirmed) return true;
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

  /** 主模型 think（reasoning_chunk 不带 model）：live 可折叠 Think 行（dsh 规格）。 */
  function ensureReasoning(st) {
    if (st.reasoning) return st;
    textEntry(st);
    if (!st.textEl) return st;
    const r = makeThink("", { done: false });
    st.textEl.insertBefore(r, st.textBody);
    st.reasoning = r;
    st.reasoningText = r.querySelector(".think-body");
    st.reasoningSum = r.querySelector(".think-sum");
    st._thinkT0 = performance.now();
    const timeEl = r.querySelector(".think-time");
    st._thinkTimer = setInterval(() => {
      if (!st._thinkT0) return;
      timeEl.textContent = ((performance.now() - st._thinkT0) / 1000).toFixed(1) + "s";
    }, 200);
    return st;
  }

  /** think 收尾：停走秒、头行转已完成态（摘要由「最新一行」切到「第一行」）。 */
  function finishReasoning(st) {
    if (!st.reasoning) return;
    if (st._thinkTimer) { clearInterval(st._thinkTimer); st._thinkTimer = null; }
    if (st._thinkT0) {
      const s = ((performance.now() - st._thinkT0) / 1000).toFixed(1);
      st._thinkT0 = 0;
      const t = st.reasoning.querySelector(".think-time");
      if (t) t.textContent = s + "s";
      st.reasoning.classList.remove("thinking");
      st.reasoning.classList.add("done");
      if (st.reasoningSum) {
        st.reasoningSum.textContent = thinkSummary(st.reasoningText ? st.reasoningText.textContent : "", false);
      }
    }
  }

  function appendChunk(st, kind, text, seq, model) {
    if (!text) return;
    if (kind === "reasoning") {
      // 带 model 的是三席思考（并发交织）→ 实时落进该轮 seats 节点（装配器内合流）；
      // 不带 model 的是主模型思考 → 落 live 正文条目内的 Think 行。
      if (model) {
        applyLiveFrame(st, {
          seq: seq, type: "reasoning", pair_id: st.liveRunId,
          turn: 0, data: { text: text, model: model },
        });
        return;
      }
      ensureReasoning(st);
      if (!st.reasoningText) return;
      st.reasoningText.textContent += text;
      if (st.reasoningSum) st.reasoningSum.textContent = thinkSummary(st.reasoningText.textContent, true);
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
    st.reasoningSum = null;
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
      st.reasoning = null; st.reasoningText = null; st.reasoningSum = null;
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

  /** 收流：只关两路 SSE，**保留渲染状态**（装配器 ＋ 组视图 ＋ DOM 归属）。
      状态一丢，下一次渲染就会把已在屏上的轮容器当成「新组」重建、长出重复容器
      （实测踩过）。换卡/换状态由 ensureRun 显式新建处理。 */
  function stop() {
    stopES();
  }

  /** 事件流是否已断（供 workspace 的断线自愈判据：健康时**绝不**做整表重建）。 */
  function streamDead(wid, cid) {
    if (!current || current.wid !== wid || current.cid !== cid) return true;
    return !!current.esDead;
  }

  /** 开始跟踪新一轮：两路 SSE。
      过程帧（/stream）→ 装配器 → 受影响轮组 upsert；
      逐字帧（/tokens）→ live 正文气泡（不进装配器）。
      onFail 在事件流连不上时回调一次（降级保底）。 */
  function start(wid, cid, onFail) {
    const st = ensureRun(wid, cid);
    stopES();
    st.liveRunId = "";        // 新一轮：轮锚随 run_start 帧到达后才落
    st.settled = false;       // 解冻：上一轮的收口状态不带到这一轮
    st.esDead = false;
    try {
      // 无游标（lastSeq=0）＝从当前末帧起；有游标＝断点续传。
      const sFrom = st.lastSeq > 0 ? st.lastSeq + 1 : 0;
      const es = new EventSource(`/api/works/${wid}/cards/${cid}/stream?from=${sFrom}`);
      st.es = es;
      es.onopen = () => { st.esDead = false; };
      es.onmessage = (m) => {
        try {
          const frame = JSON.parse(m.data);
          if (typeof frame.seq === "number" && frame.seq > st.lastSeq) st.lastSeq = frame.seq;
          if (frame.type === "done" || frame.type === "error") finish();
          applyLiveFrame(st, frame);
        } catch (e) { /* 单帧坏不致命 */ }
      };
      es.onerror = () => {
        st.esDead = true;
        if (!st.fallbackUsed) {
          st.fallbackUsed = true;
          if (typeof onFail === "function") onFail();
        }
      };
    } catch (e) {
      st.esDead = true;
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
  optimisticUser, dropOptimisticUser, streamDead,
};
