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
import { API } from "./esm/api.js?v=37";
import { escapeHtml as esc } from "./esm/util.js?v=30";
/* [边出边渲染 2026-10-14 · 用户令 ⑧] 流式期间也要渲染 md */
import { renderMarkdown } from "./esm/md.js?v=31";
import { createAssembler } from "./esm/assembler.js?v=39";
import { createChatView } from "./esm/flow/chat-view.js?v=5";
import { createMessageItem } from "./esm/flow/message-item.js?v=1";
// [5715 · 性能] 思考块的增量渲染（直播路径 O(N²) → O(N)）
import { createThinkRenderer, createTextUpdater } from "./esm/flow/think-stream.js?v=1";
import { createCopyButton, createResendButton } from "./esm/flow/message-actions.js?v=1";

/* [画板四机制 T1 2026-10-16] 会话表：一块画板＝**一张卡**＋它的全部状态。
   键 `${wid}/${cid}`；字段即原 `current` 单例的原样搬运（asm/view/liveSlot/
   settled/lastSeq…）＋ 画板容器 `board` ＋ 滚动位置 `scrollTop`。
   只有 activeKey 指向的会话，其画板（.msgs）挂在 #chatArea；其余会话的画板是
   **游离节点**（不在文档流）——切走的卡后台照写自己那份，结构上不可能写进
   当前卡（治 P4 串卡）。原单例 `let current` 随之退役（不再有「全局唯一画板」）。 */
const sessions = new Map();      // key → session
let activeKey = null;            // 当前在看的会话键（null＝还没激活任何会话）

function skey(wid, cid) { return wid + "/" + cid; }

/* [画板四机制 T2/T3 2026-10-16] 画板可调参数（`window.__FLOW_BOARD__` 可覆盖；生产不设＝默认）。
   pageNodes     —— 分页：每页**节点数**（[对话流照抄 dsh v3] 扁平节点表后单位由「轮」改「节点」）。
   residentMax   —— T3 常驻硬上限：同时留内存的画板数（LRU 超出即丢最旧）。
   idleReleaseMs —— T3 静默超时：多久没用就释放。
   sweepMs       —— T3 释放扫描周期。 */
const BOARD_CFG = Object.assign(
  { pageNodes: 120, residentMax: 8, idleReleaseMs: 10 * 60 * 1000, sweepMs: 20000 },
  (typeof window !== "undefined" && window.__FLOW_BOARD__) || {}
);

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

  /* [画板四机制 T1 2026-10-16] 统一内容列＝**本会话自己的**画板容器。
     [对话流照抄 dsh v3] 容器里装的是 flow/chat-view 的视图骨架（滚动口/跟随/
     回底/运行条），本文件只负责「节点键 → 元素」。 */
  function boardOf(st) {
    if (!st) return null;
    if (!st.board) viewOf(st);
    return st.board;
  }

  function msgsEl(st) {
    return boardOf(st);
  }

  /* [对话流照抄 dsh v3 · §三 退役清单] 原 scrollBottom(st)（每帧强制滚到底）
     已删除：滚动跟随改由 flow/chat-view 的 24px 阈值 ＋ 读者滚动台账负责
     （ChatView.tsx:264-299），程序不再无脑抢滚动位置。 */

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

  /* [5715 · 性能] 思考渲染器的**构造**搬进 esm/flow/think-stream.js：直播时每帧
     思考都会让那一行重渲染，而行里含**累积全文** ⇒ 一次思考 N 帧 ⇒ 写入量 O(N²)
     ⇒ 长思考把主线程打满（页面卡死的一半原因；另一半在落盘侧，已由
     event_stream 的「按轮合并落一条」解决）。
     新件在文本是**前缀扩展**时只追加增量（O(增量)），并保留用户的折叠态。 */
  const _thinkRenderer = createThinkRenderer({ summaryOf: thinkSummary });
  /* [5715 · 性能] 席位并排卡里的思考（``.tk``）同样是逐帧增长的整块文本 ⇒
     同样走增量文本器（否则三席并发时又是一个 O(N²)）。 */
  const _seatText = createTextUpdater();

  /** 三席并排卡片（seats 节点：三席各一卡，思考与输出同卡）。 */
  function makeSeats(members, nodeKey) {
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
      // [5715 · 性能] 席位的思考也是**逐帧增长**的整块文本 ⇒ 整块重写同样是 O(N²)
      // ⇒ 走增量文本器（键＝节点键+席位键）。
      _seatText.set((nodeKey || "seats") + "|" + key,
        card.querySelector(".tk"), m.thinking || "");
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
          /* step_id 不编进主文案：它已在本行细节里（fld step_id），主文案再说一遍
             「步 step_x 完成」＝同一句要在两处出现，且主文案还被更多人看到。 */
          (done ? "全部步骤完成，计划收口" : "步骤完成") +
          fld("step_id", d.step_id) +
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
        // 0KB 时不再显示体积（拼接为空是常态，挂个 0KB 只是噪音）
        return makeRow(t, "交付拼接" + fld("branch", d.branch) + fld("bytes", d.bytes),
          (d.bytes || 0) > 0 ? Math.round((d.bytes || 0) / 1024) + "KB" : null);
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
          null, fmtData(d), "lv3")];
      case "context_injection": {
        const ref = d.source_ref || {};
        const refTxt = (typeof ref === "string")
          ? ref
          : [ref.script_name || ref.plan_name || "", ref.step_id || ""].filter(Boolean).join(" · ");
        return [makeRow(node.type,
          "上下文注入·当前步骤 " + esc(refTxt) +
          fld("source_kind", d.source_kind) + fld("step_id", d.step_id) +
          fld("index", d.index) + fld("total", d.total),
          null, fmtData(d), "lv3")];
      }
      case "judgment_protocol":
        return [makeRow(node.type, "判断协议（轮首注入）" + fld("source_kind", d.source_kind),
          null, fmtData(d), "lv3")];
      case "context_seat_identity":
        return [makeRow(node.type, "三席身份注入" + fld("source_kind", d.source_kind),
          null, fmtData(d), "lv3")];
      // [更新现状按钮改造 2026-10-16] 「更新现状」专用回合的轮首任务注入。
      case "status_update_task":
        return [makeRow(node.type, "现状更新（轮首注入）" + fld("source_kind", d.source_kind),
          null, fmtData(d), "lv3")];
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
      /* [5702 · C-1] 不截断（dsh tool-node-reader 读全文）：事件层早已无 8KB 闸，
         这里再切 4000 字＝用户看不到完整结果。折叠态不构建，展开才付代价。 */
      c.textContent = String(d.content);
      out.push(c);
    }
    if (Array.isArray(d.files) && d.files.length) {
      const ul = document.createElement("ul");
      ul.className = "tr-files";
      for (const f of d.files) {
        const li = document.createElement("li");
        li.textContent = String(f);
        ul.appendChild(li);
      }
      out.push(ul);
    }
    if (Array.isArray(d.items) && d.items.length) {
      const ul = document.createElement("ul");
      ul.className = "tr-files";
      for (const it of d.items) {
        const li = document.createElement("li");
        li.textContent = typeof it === "object" ? fmtData(it) : String(it);
        ul.appendChild(li);
      }
      out.push(ul);
    }
    // 其余键值对逐行（不含已提取的）
    const rest = {};
    const objs = [];
    for (const k of Object.keys(d)) {
      if (k === "ok" || k === "error" || k === "content" || k === "files" || k === "items") continue;
      if (d[k] === null || d[k] === "") continue;
      /* [5715 · 修] 对象型字段原来**整条丢弃**（continue）⇒ 工具结果里只有对象字段时
         展开体是**一片空白**（实测 create_card 的结果只有 card 对象 ⇒ 点开什么都没有，
         用户以为点坏了）。改为给一段截断 JSON 摘要。 */
      if (typeof d[k] === "object") { objs.push([k, d[k]]); continue; }
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
    for (const pair of objs) {
      const o = document.createElement("div");
      o.className = "tr-kv tr-kv-obj";
      /* 截断到 2000 字：对象字段是附属信息（正文在 content 字段，不截断），
         整份 card/meta JSON 可能上万字，全量倒出来会淹没结果区。 */
      o.innerHTML = "<span class='f'><span class='k'>" + esc(pair[0]) + "</span> = <span class='v'>"
        + esc(clip(fmtData(pair[1]), 2000)) + "</span></span>";
      out.push(o);
    }
    /* [5715 · 修] 实在没有可展示的字段 ⇒ 明说"没有"，别留一片空白让人以为点坏了。 */
    if (!out.length) {
      const e = document.createElement("div");
      e.className = "tr-empty";
      e.textContent = "（该结果没有可展示的字段）";
      out.push(e);
    }
    return out;
  }

  /** 工具参数摘要（dsh ToolRow 的 FILL 截断摘要）。
      正文类字段不参与摘要：把 content 摊在工具行里既读不懂、又把真正的
      关键参数（path / card_id / title）挤没了。 */
  const ARG_SKIP = new Set(["content", "body", "text", "mission", "status_block", "rulings", "requirements", "prompt"]);
  function argSummary(args) {
    if (!args || typeof args !== "object") return "";
    const parts = Object.keys(args)
      .filter((k) => !ARG_SKIP.has(k))
      .map((k) => k + "=" + args[k]);
    return clip(parts.join(" "), 76);
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
    const META_KEYS = new Set(["ok", "tool", "target", "card_id", "tool_call_id", "call_id", "duration", "at", "ts"]);
    const rk = Object.keys(d).filter(k =>
      !META_KEYS.has(k) && typeof d[k] !== "object" && d[k] !== "" && d[k] != null);
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
      sub.innerHTML = '<span class="mk ' + (ok ? "ok" : "no") + '" title="' + (ok ? "成功" : "失败") + '"></span>' +
        (node.duration ? esc((node.duration / 1000).toFixed(1) + "s") : "");
    }
    /* 结果摘要：只有**有实质内容**时才占第二行。
       全是「完成」的工具行（read_card/edit_card 这些）若每个都挂一行「完成」，
       工具区会变成一片 48px 的空壳卡片——状态点在第一行已经说了。 */
    const summary = resultSummary(res, hasResult, ok);
    if (summary !== "完成" && summary !== "失败" && summary !== "调用中…") {
      const sum = document.createElement("div");
      sum.className = "tool-sum";
      sum.textContent = summary;
      row.appendChild(sum);
    }
    // 完整内容：默认折叠（点整行展开）；**首次展开才构建**（dsh 两段式）。
    // 折叠态不预构建的原因：工具结果不截断（见 renderResultContent），
    // 大结果若在折叠时就建 DOM，等于白烧一次。
    const body = document.createElement("div");
    body.className = "tool-body" + (ok ? "" : " err");
    let bodyBuilt = false;
    row.appendChild(body);
    row.classList.add("has-detail");
    row.onclick = (e) => {
      if (e.target.closest(".tool-body")) return;
      if (!bodyBuilt) {
        bodyBuilt = true;
        for (const p of renderResultContent(res.result || res)) body.appendChild(p);
      }
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
    if (Object.keys(members).length) out.push(makeSeats(members, node.key));
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

  /** workspace 注入富消息渲染器（md 渲染 / 卡间信头剥离 / 元信息行）。
      [对话流照抄 dsh v3] user 口改成 `userHtml(text)`：气泡**外壳**归
      flow/message-item（dsh MessageItem 的几何原值），正文 HTML 仍由本项目格式化。 */
  function setMessageRenderers(m) {
    if (m && m.userHtml && m.assistant && m.cardmsg) _msgRenderers = m;
  }

  /** 用户气泡＝flow/message-item（dsh MessageItem 移植件）。 */
  const _messageItem = createMessageItem({
    renderUserHtml: (text) => (_msgRenderers.userHtml ? _msgRenderers.userHtml(text) : esc(text)),
  });

  function withDate(node, elOrEls) {
    if (!node || !node.__showDate) return elOrEls;
    const div = dateDivider(node.ts);
    return [div].concat(Array.isArray(elOrEls) ? elOrEls : [elOrEls]);
  }

  function renderUser(node) {
    const row = _messageItem.renderUser(node);
    if (node.pending) row.classList.add("is-pending");   // 乐观上屏未认领

    /* [对话界面商业化 2026-10-17] 用户消息操作条（hover 浮出）：
       复制 + 重新发送。重发不直接调 workspace——stream 不持有发送闭包，
       用一次自定义事件把「用户要求重发这段原文」交给宿主，避免反向依赖。
       [5715 · 抽模块] 两个按钮的逻辑搬进 esm/flow/message-actions.js
       （原先长在渲染闭包里 ⇒ 不可单测、且剪贴板回退逻辑写了两份）。 */
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    const bubble = row.querySelector(".flow-user-bubble");

    const t = document.createElement("span");
    t.className = "msg-time";
    const when = fmtClock(node.ts);
    if (when) { t.textContent = when; t.title = String(node.ts); actions.appendChild(t); }

    const getUserText = () => (bubble ? bubble.innerText : "");
    actions.appendChild(createCopyButton({ getText: getUserText, title: "复制" }));

    if (!node.pending) {
      actions.appendChild(createResendButton({ getText: getUserText }));
    }

    /* 挂进气泡容器而不是行容器：行是全宽 flex 列，挂行上只能排在气泡下方，
       会**永久占掉 24px**（hover 只是把它变透明），于是每条用户消息下面都多一段空。
       挂进 .flow-user-stack（宽度＝气泡宽度）后可用 right:100% 贴到气泡左侧，零占位。 */
    const stack = row.querySelector(".flow-user-stack");
    (stack || row).appendChild(actions);
    return withDate(node, row);
  }

  /** 时刻文案（用户消息 hover 用）：只给 HH:MM，日期由分隔线承担。 */
  function fmtClock(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";
    const pad = (n) => String(n).padStart(2, "0");
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function renderAssistant(node) {
    const el = _msgRenderers.assistant(node);
    // 思考块插在正文之前（位置＝数据的函数：思考属本轮 assistant）
    if (node.reasoning) {
      const md = el.querySelector(".md");
      // [5715 · 性能] 走增量渲染器：直播时每帧只**追加**新增部分（不重写整块）。
      const think = _thinkRenderer.row(node.key, node.reasoning, { done: true });
      if (md) el.insertBefore(think, md); else el.appendChild(think);
    }
    return withDate(node, el);
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
     三、快照渲染：扁平节点表（dsh chat-snapshot-builder 形态）
     —— 一切可见物＝一个节点座位，order 是唯一位置来源。
        结构变了（order 引用变了）⇒ 全序落位；只有内容变了 ⇒ 只换那一行。
        **没有任何一处读 DOM 现状做位置推算**；既有座位永不因别处变化被搬动。
     [对话流照抄 dsh v3 · §三 退役] 轮分组三道（user→过程→reply）、组视图
        ensureGroupView / renderGroup / pruneGroups / 组级折叠开关，全部删除。
     ================================================================== */

  /** 每会话的视图骨架（滚动口 / 24px 跟随 / 回底钮 / 运行条 / 分页）。 */
  function viewOf(st) {
    if (st.view) return st.view;
    const holder = document.createElement("div");
    holder.className = "msgs";
    st.board = holder;
    st.view = createChatView({
      mount: holder,
      renderRow: (key) => {
        const node = st.asm.snapshot().nodes.get(key);
        return node ? renderNode(node) : [];
      },
      onLoadOlder: () => { loadEarlier(st); },
      runningStart: () => st.turnStart,
      isRunning: () => !!st.busy,
      hasMore: () => (st.winStart || 0) > 0,
      isLoadingOlder: () => false,
    });
    st.view.setUserKeyTest((key) => {
      const node = st.asm.snapshot().nodes.get(key);
      return !!node && node.kind === "user";
    });
    return st.view;
  }

  /** 分页窗口起点（单位＝节点）。窗口下端恒为最后一节点（新节点追加不裁切）。 */
  function winStartOf(st, total) {
    let start = (st.winStart == null) ? Math.max(0, total - BOARD_CFG.pageNodes) : st.winStart;
    if (start >= total) start = Math.max(0, total - BOARD_CFG.pageNodes);
    st.winStart = start;
    return start;
  }

  /** 往前插一页：锚点由 chat-view 在点击时先记好，这里只改窗口并重画。 */
  function loadEarlier(st) {
    if (!st || !st.winStart || st.winStart <= 0) return false;
    st.winStart = Math.max(0, st.winStart - BOARD_CFG.pageNodes);
    renderSnapshot(st, st.asm.snapshot());
    return true;
  }

  /** 取某节点的内容版本（渲染层据此判「要不要换这一行」）。 */
  function revGetter(snap) {
    return (key) => {
      const n = snap.nodes.get(key);
      return n ? (n.rev || 0) : null;
    };
  }

  /** 全量快照：复用既有座位与既有元素，只建缺的、只换变了的。 */
  /* ── 日期分隔 ──────────────────────────────────────────────────────────
     跨天的会话没有日期锚，用户翻到中间不知道「这是哪天说的」。
     在装配快照上按节点 ts 标一个 __showDate（同一天只在首个节点显示），
     渲染层据它前置一条分隔线。标记打在节点上而不是 DOM 上：
     实时增量只重建单行时，标记仍然在，不会因为重建而丢。 */
  function dayKey(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";
    return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
  }

  function dayLabel(ts) {
    const d = new Date(ts);
    const now = new Date();
    const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
    if (same(d, now)) return "今天";
    const y = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    if (same(d, y)) return "昨天";
    if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + "月" + d.getDate() + "日";
    return d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日";
  }

  function markDates(snap) {
    let prev = "";
    for (const k of snap.order) {
      const n = snap.nodes.get(k);
      if (!n) continue;
      const d = dayKey(n.ts);
      n.__showDate = !!d && d !== prev;
      if (d) prev = d;
    }
  }

  function dateDivider(ts) {
    const el = document.createElement("div");
    el.className = "date-divider";
    el.innerHTML = "<span></span>";
    el.firstChild.textContent = dayLabel(ts);
    return el;
  }

  function renderSnapshot(st, snap) {
    const v = viewOf(st);
    st._dirty = null;                 // 结构重建：作废尚未落地的增量刷新
    const total = snap.order.length;
    const start = winStartOf(st, total);
    markDates(snap);
    const win = snap.order.slice(start);
    v.render(win, revGetter(snap));
    v.paintOlder();
    v.paintStatus();
    // [5715 · 性能] 思考渲染器的缓存只留**当前还挂着的节点**（防持有旧元素）
    _thinkRenderer.prune(new Set(snap.order));
    st.renderedOrder = snap.order;
    logInfo("[flow-render] snapshot(" + win.length + "/" + total + " nodes)");
  }

  /** 实时增量：结构没变（order 引用未变）⇒ 只换受影响那一行。
      [5702 · C-1] 内容更新按**帧**合并（dsh `publication:"animation-frame"`）：
      逐字流一帧一条，若每条都重渲染 Markdown，主线程会被打满、观感「一顿一顿」。
      同一帧内多次更新只落一次——这是「流畅」的来源。 */
  function renderAffected(st, node) {
    if (!st || !st.asm) return;
    const snap = st.asm.snapshot();
    if (st.renderedOrder !== snap.order) { renderSnapshot(st, snap); return; }
    if (!node) return;
    const idx = snap.order.indexOf(node.key);
    if (idx < 0) return;
    if (idx < (st.winStart || 0)) return;     // 窗口之上：不进 DOM
    if (!st._dirty) st._dirty = new Map();
    st._dirty.set(node.key, node.rev || 0);
    if (st._raf) return;
    const flush = () => {
      st._raf = null;
      const dirty = st._dirty || new Map();
      st._dirty = null;
      const v = viewOf(st);
      for (const [k, rev] of dirty) v.refresh(k, rev);
      v.paintStatus();
    };
    st._raf = (typeof requestAnimationFrame === "function")
      ? requestAnimationFrame(flush)
      : setTimeout(flush, 16);
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
      /* [对话界面商业化 2026-10-17] 帧时间：后端 emit 的 ts 是秒级 float，
         统一折成 ISO 串——与落盘记录同一形态，刷新前后渲染走同一条路径。 */
      ts: frame && frame.ts
        ? (typeof frame.ts === "number" ? new Date(frame.ts * 1000).toISOString() : String(frame.ts))
        : "",
    };
  }

  /** 取/建「某张卡自己的会话」。**不再 stop() 踢人**：切走的卡保留自己的
      装配器/组视图/画板/事件流句柄，后台照写自己那份（T1）。 */
  function ensureRun(wid, cid) {
    const key = skey(wid, cid);
    let st = sessions.get(key);
    if (st) return st;
    st = {
      wid, cid, key,
      es: null, esT: null,
      board: null,              // 本会话画板（.msgs）；未激活时游离
      scrollTop: 0,             // 切走时的滚动位置（切回恢复）
      lastUsed: Date.now(),     // [T3] 最近使用时刻（LRU/静默超时判据）
      busy: false,              // [T3] 本轮在跑（start→resync/stop 之间）——在跑卡永不释放
      asm: createAssembler(), view: null, liveRunId: "", settled: false,
      /* [对话流照抄 dsh v3] 扁平节点表的分页窗口与「上一帧的 order 引用」：
         winStart      = 窗口起始**节点下标**
         renderedOrder = 上次落位时的 order 数组引用（引用未变＝结构未变）
         turnStart     = 本轮开始时刻（运行条 15s 时钟的锚） */
      winStart: null, renderedOrder: null, turnStart: null,
      fallbackUsed: false, esDead: false, lastSeq: 0, lastTokenSeq: 0,
      /* [T5 2026-10-16] 两套 seq 分家，**严禁互写**：
         lastSeq   = 内存 journal seq（SSE 续传游标；event_stream._seq_counters，重启归零）
         diskSeq   = 已见的最大**落盘** events seq（event_log 从文件尾 +1 续号，重启不归零）
         pullSeq   = pullUserBubble 的增量游标（**落盘空间**；初值播种自 diskSeq） */
      diskSeq: 0, pullSeq: null,
    };
    sessions.set(key, st);
    return st;
  }

  /** 激活某会话：把它的画板挂到 #chatArea，并把上一个会话的画板摘下为游离节点
      （装配状态、组视图、滚动位置全留——不重建）。**同步执行**：点下去画板立刻翻页。 */
  function activate(wid, cid) {
    const prevKey = activeKey;
    const key = skey(wid, cid);
    const st = ensureRun(wid, cid);
    const ca = chatEl();
    if (activeKey === key) {
      // 已是当前会话：确保画板在位（首帧/状态重建后可能尚未挂）
      if (ca && st.board && st.board.parentNode !== ca) ca.appendChild(st.board);
      st.lastUsed = Date.now();
      return st;
    }
    if (activeKey) {
      const prev = sessions.get(activeKey);
      if (prev && prev.board) {
        if (ca) prev.scrollTop = ca.scrollTop;                 // 记下滚动位置
        if (prev.board.parentNode) prev.board.parentNode.removeChild(prev.board);   // 摘下
      }
    }
    const board = boardOf(st);
    if (ca) {
      // #chatArea 只承载当前会话的画板：游离画板不进文档流（不变量 1）
      for (const ch of Array.from(ca.children)) if (ch !== board) ca.removeChild(ch);
      ca.appendChild(board);
      void ca.scrollHeight;               // [T3] 先强制布局，再落滚动位置（否则会被重置为 0）
      ca.scrollTop = st.scrollTop || 0;   // 切回恢复滚动位置
    }
    activeKey = key;
    st.lastUsed = Date.now();
    // 换卡（prevKey 非空且不同）：整块淡入，明确「这是另一张卡」
    if (prevKey && st.view && typeof st.view.playEnter === "function") st.view.playEnter();
    return st;
  }

  /** 取某会话的内容列（供 workspace 把系统行写进**该卡自己的**容器）。
      [对话流照抄 dsh v3] 返回的是视图的**节点列**（.flow-view-column），
      系统行因此落在对话流内部、而不是对话流外面。
      缺 cardId 时回落当前会话。 */
  function board(wid, cid) {
    if (wid && cid) return viewOf(ensureRun(wid, cid)).columnEl;
    const st = activeKey ? sessions.get(activeKey) : null;
    return st ? viewOf(st).columnEl : null;
  }

  /* ==================================================================
     [画板四机制 T3 2026-10-16] 常驻与释放
     —— 加载过的画板留内存；切换＝挂上/摘下（activate），**不重建** ⇒ 切回即时、滚动位置还在。
        释放＝静默超时 ＋ LRU 硬上限。
        **铁律：当前在看的不释放；有任务在跑的不释放。**
        释放只丢 DOM ＋ 装配状态；数据在 timeline（不变量 10），切回按 timeline 重建（兜底）。
     ================================================================== */

  /** 有任务在跑：本轮在飞（busy）或事件流还开着——二者任一即视为在跑。 */
  function isBusy(st) {
    return !!st && (!!st.busy || !!st.es || !!st.esT);
  }

  /** 释放一个会话：只丢 DOM ＋ 装配状态，不丢数据。铁律两道闸都在这里。 */
  function releaseSession(key) {
    const st = sessions.get(key);
    if (!st) return false;
    if (key === activeKey) return false;   // 当前在看：永不释放
    if (isBusy(st)) return false;          // 有任务在跑：永不释放
    stopES(st);
    if (st.board && st.board.parentNode) st.board.parentNode.removeChild(st.board);
    sessions.delete(key);
    logInfo("[flow-board] release " + key);
    return true;
  }

  /** 释放扫描：① 静默超时 ② 硬上限（LRU，最旧先丢）。返回扫描后的常驻数。 */
  function sweepSessions() {
    const now = Date.now();
    for (const [key, st] of Array.from(sessions)) {
      if (key === activeKey || isBusy(st)) continue;
      if (now - (st.lastUsed || 0) >= BOARD_CFG.idleReleaseMs) releaseSession(key);
    }
    let over = sessions.size - BOARD_CFG.residentMax;
    if (over > 0) {
      const idle = Array.from(sessions.keys())
        .filter((k) => k !== activeKey && !isBusy(sessions.get(k)))
        .sort((a, b) => (sessions.get(a).lastUsed || 0) - (sessions.get(b).lastUsed || 0));
      for (const k of idle) {
        if (over <= 0) break;
        if (releaseSession(k)) over--;
      }
    }
    return sessions.size;
  }

  /* 扫描定时器。Node/jsdom 下 setInterval 返回 Timeout（有 unref）——unref 掉，
     免得取证脚本被这个「永不清理」的定时器吊住不退出；浏览器返回数字、无 unref，照常跑。 */
  const _sweepTimer = setInterval(sweepSessions, Math.max(50, BOARD_CFG.sweepMs));
  if (_sweepTimer && typeof _sweepTimer.unref === "function") _sweepTimer.unref();

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
    renderAffected(st, node);
  }

  /** 乐观上屏（T1）：点发送瞬间把用户气泡落进扁平表，不等 POST/timeline。 */
  let _localSeq = 0;
  function optimisticUser(wid, cid, content) {
    const st = ensureRun(wid, cid);
    const localKey = "u" + (++_localSeq);
    const node = st.asm.applyRecord({
      ns: "local", seq: 0, local_key: localKey, type: "user_message",
      run_id: "", round: 0, data: { content: String(content == null ? "" : content) }, model: "",
    });
    renderAffected(st, node);
    return localKey;
  }

  /** 发送失败回退：撤下未被认领的本地轮（幽灵气泡不留在屏上）。 */
  function dropOptimisticUser(wid, cid) {
    const st = ensureRun(wid, cid);
    if (st.asm.dropLocal()) renderSnapshot(st, st.asm.snapshot());
  }

  /** 刷新/换卡恢复：GET /timeline → rebuild → renderSnapshot。
      ★ 断 timeline 即会话区空——单源单通道的代价与证据（判据2）：
      这里**不做任何第二数据源兜底**，也不保留上一轮的 DOM。 */
  async function replayTrace(wid, cid) {
    // [T1] 前台「打开/换卡恢复」＝激活该卡画板（open() 已先行同步 activate，此处幂等）。
    // 后台结算回流走 resync()——**不激活**，结果落该卡自己的（可能游离的）画板（§五.5）。
    const st = activate(wid, cid);
    stopES(st);
    /* [5715 · 修] 骨架必须在**任何**结局下收起：原来 markLoaded 只在末尾调，
       fetchTimeline 抛错 ⇒ 骨架永久停留（用户看到"一直在加载"却什么也没发生）。 */
    try {
      const items = await fetchTimeline(st, wid, cid);
      renderSnapshot(st, st.asm.rebuild(items));
    } finally {
      if (st.view && typeof st.view.markLoaded === "function") st.view.markLoaded();
    }
  }

  /** 结算后回流：与刷新同一条路（唯一数据源仍是 timeline）。
      POST 返回＝本轮事件已全部落盘，此刻拉 timeline 必然含 assistant_text。
      返回 {ok, done}：done＝最后一轮已出现 assistant 节点或 done/error 收口
      （workspace 的断线自愈轮询据此判「已结算」，不再读 /session 数条数）。 */
  async function resync(wid, cid) {
    const st = ensureRun(wid, cid);
    const items = await fetchTimeline(st, wid, cid);
    st.settled = true;           // 收口：此后到达的实时帧不再入装配（见 applyLiveFrame 注）
    /* [5702 · 2026-10-20] 实时表**已完整**（SSE 活着且已收到 done/error）⇒ 不重建。
       重建会把运行中按轮分开的 assistant 节点抹成落盘的一条（后端每 run 只落一条
       assistant_text）——文本不丢但结构塌陷。只有「流断了 / 实时没收到收口」才重建。 */
    const liveSnap = st.asm.snapshot();
    const liveComplete = !st.esDead && liveSnap.order.some((k) => {
      const n = liveSnap.nodes.get(k);
      return n && n.ns === "live" && (n.type === "done" || n.type === "error");
    });
    const snap = liveComplete ? liveSnap : st.asm.rebuild(items);
    renderSnapshot(st, snap);
    if (st.view && typeof st.view.markLoaded === "function") st.view.markLoaded();
    setRunning(wid, cid, false); // 结算＝本轮结束（运行条收、主键回发送态）
    // 收口判据：本轮已出现 assistant 节点，或已见 done/error
    const done = snap.order.some((k) => {
      const n = snap.nodes.get(k);
      return n && (n.kind === "assistant" || n.type === "done" || n.type === "error");
    });
    return { ok: items !== null, done, runId: st.liveRunId };
  }

  /** 拉 timeline。`from` 省略＝全量（刷新/结算）；给值＝只取 seq>=from 的增量（T5）。
      ★ 无论全量还是增量，都把本次 items 的**末条 seq** 记进 `st.diskSeq`（落盘水位）——
      这是增量游标唯一的播种源；**绝不与 `st.lastSeq`（内存 journal seq）互写**。 */
  async function fetchTimeline(st, wid, cid, from) {
    try {
      const resp = await API.getTimeline(wid, cid, from);
      const items = (resp && resp.items) || [];
      if (st && items.length) {
        const last = items[items.length - 1];
        if (last && typeof last.seq === "number") {
          st.diskSeq = Math.max(st.diskSeq || 0, last.seq);
        }
      }
      return items;
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
    /* [T5 2026-10-16] 增量游标播种：取「会话已见的落盘水位」与既有游标的较大者。
       ★ 任务书 §六 T5 原写「初值＝`st.lastSeq`」——那是**内存 journal seq**，与 /timeline
         的落盘 seq 是两个空间（journal 重启归零、落盘从文件尾续号），用它当起点会
         ①首次（lastSeq 常为 0）仍是整卡全量拉；②同进程第二轮起 lastSeq 可能大于落盘
         seq ⇒ 拉取返回空、**漏掉要找的那条 user_message**。故改用 `st.diskSeq`。
         详见 `_画板四机制_T5_施工回报_20261016.md`。 */
    st.pullSeq = Math.max(st.pullSeq || 0, st.diskSeq || 0);
    for (let i = 0; i < 6; i++) {
      // 每次循环只拉游标**之后**的新记录：首拉通常只有本条 user_message 一两条，
      // 不再是整卡历史（P6）。6×500ms 循环保留＝SSE 断连时的对账兜底。
      const items = await fetchTimeline(st, wid, cid, st.pullSeq);
      if (items === null) return false;
      // 游标推进：取本次 items 末条 seq（后端按 seq 升序返回）
      if (items.length) {
        const last = items[items.length - 1];
        if (last && typeof last.seq === "number" && last.seq > st.pullSeq) st.pullSeq = last.seq;
      }
      let confirmed = false;
      for (const rec of items) {
        if (!rec || rec.type !== "user_message") continue;
        const node = st.asm.applyRecord(Object.assign({ ns: "disk" }, rec));
        renderAffected(st, node);
        if (node && node.kind === "user" && !node.pending) confirmed = true;
      }
      if (confirmed) return true;
      await new Promise((r) => setTimeout(r, 500));
    }
    return false;
  }

  /* ==================================================================
     五、逐字流：/tokens 帧 → 装配器（dsh `assistant/chunk` 语义）
     —— 正文按 run:round 累积进**同一个** assistant 节点；轮界只换轮次、
        **不清字**；渲染走统一节点渲染器（workspace 注入的 assistant 渲染器）。
        旧「旁挂 live 气泡 ＋ 100ms 定时重排 ＋ round_start 清字」整段退役：
        它正是「中间轮文字被吞 / 流式与落盘两套数据」的根因。
     ================================================================== */

  function onTokenFrame(st, evt) {
    if (evt && typeof evt.seq === "number" && evt.seq > st.lastTokenSeq) st.lastTokenSeq = evt.seq;
    const type = (evt && evt.type) || "";
    if (!type) return;
    /* [5702 · C-1] 轮界帧只更新轮锚（后端 emit 已写），**不再清字**——清字是
       「中间轮文字被吞」的直接原因。正文/思考增量统一走装配器：
       按 run:round 累积进同一个 assistant 节点（dsh assistant/chunk 语义）。 */
    if (type === "run_start" || type === "round_start") return;
    if (type === "content_chunk" || type === "reasoning_chunk") {
      applyLiveFrame(st, evt);
    }
  }

  /* ==================================================================
     六、流启停
     ================================================================== */

  function stopES(st) {
    if (!st) return;
    if (st.es) { try { st.es.close(); } catch (e) { /* noop */ } st.es = null; }
    if (st.esT) { try { st.esT.close(); } catch (e) { /* noop */ } st.esT = null; }
  }

  /** 收流：只关两路 SSE，**保留渲染状态**（装配器 ＋ 节点座位 ＋ DOM 归属）。
      [T1] 换卡不再经过这里——会话表按 (wid,cid) 各留一份状态，
      「摘下画板」由 activate() 负责；本函数只管停流。可传 (wid,cid) 精确停某会话；
      不传＝停当前会话（向后兼容既有调用点）。 */
  function stop(wid, cid) {
    if (wid && cid) {
      const st = sessions.get(skey(wid, cid));
      if (st) { stopES(st); setRunning(wid, cid, false); }
      return;
    }
    const st = activeKey ? sessions.get(activeKey) : null;
    if (st) { stopES(st); setRunning(st.wid, st.cid, false); }
  }

  /** 运行态：一条入口管「在跑卡不释放」「运行条」「主键两态」三处。 */
  function setRunning(wid, cid, running) {
    const st = ensureRun(wid, cid);
    st.busy = !!running;
    if (running) st.turnStart = Date.now();
    const v = viewOf(st);
    v.setRunning(!!running);
    v.paintStatus();
    return st;
  }

  /** 事件流是否已断（供 workspace 的断线自愈判据：健康时**绝不**做整表重建）。 */
  function streamDead(wid, cid) {
    const st = sessions.get(skey(wid, cid));
    if (!st) return true;
    return !!st.esDead;
  }

  /** [5702 · 2026-10-20] 某卡是否在跑（按卡查，供输入系统切卡时恢复运行态）。 */
  function sessionRunning(wid, cid) {
    const st = sessions.get(skey(wid, cid));
    return !!(st && st.busy);
  }

  /** 开始跟踪新一轮：两路 SSE。
      过程帧（/stream）→ 装配器 → 受影响节点 upsert；
      逐字帧（/tokens）→ live 正文气泡（不进装配器）。
      onFail 在事件流连不上时回调一次（降级保底）。 */
  function start(wid, cid, onFail, opts) {
    const st = ensureRun(wid, cid);
    const replayTokens = !!(opts && opts.replayTokens);   // [5702] 打开在跑的卡：回放本 run
    stopES(st);
    st.liveRunId = "";        // 新一轮：轮锚随 run_start 帧到达后才落
    st.settled = false;       // 解冻：上一轮的收口状态不带到这一轮
    st.esDead = false;
    setRunning(wid, cid, true);   // [T3] 本轮在跑 ⇒ 在跑卡永不释放 ＋ 运行条起
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
          // [5702] 旧 finish(st)（live 槽收尾）已随旁路退役；done/error 由装配器落节点。
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
      const tFrom = replayTokens ? 0 : (st.lastTokenSeq > 0 ? st.lastTokenSeq + 1 : 0);
      const esT = new EventSource(
        `/api/works/${wid}/cards/${cid}/tokens?from=${tFrom}${replayTokens ? "&replay=1" : ""}`);
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
  optimisticUser, dropOptimisticUser, streamDead, sessionRunning,
  // [对话流照抄 dsh v3] 运行态唯一入口：在跑卡不释放 ＋ 运行条 ＋ 主键两态
  setRunning,
  // [画板四机制 T1] 会话表对外两口：activate＝把某卡画板挂上 #chatArea（open 用）；
  // board＝取某卡自己的容器（workspace 把系统行写进该卡那份，不再写全局唯一列）。
  activate, board,
  /* [对话界面商业化 2026-10-17] 停止过渡态：停止指令已发、回合尚未收口。 */
  setStopping(wid, cid, bool) {
    const st = sessions.get(skey(wid, cid));
    if (st && st.view && typeof st.view.setStopping === "function") st.view.setStopping(bool);
  },
  // [画板四机制 T3] 释放扫描 ＋ 常驻键只读探针（供取证/联调；不改任何语义）。
  sweepSessions, residentKeys: () => Array.from(sessions.keys()),
  /* [对话界面商业化 2026-10-17] 最后一个 assistant 节点的 seq。
     send() 的异常恢复判据用它做「本轮是否真新增了回复」的前后对比——
     原判据只看「会话里有没有 assistant 节点」，历史卡恒为真，导致请求失败时
     被误判成「后端已完成、只是响应没回来」，草稿被吞、错误条不显示。 */
  assistantSeq(wid, cid) {
    const st = sessions.get(skey(wid, cid));
    if (!st) return 0;
    const snap = st.asm.snapshot();
    let mx = 0;
    for (const k of snap.order) {
      const n = snap.nodes.get(k);
      if (n && n.kind === "assistant") mx = Math.max(mx, Number(n.seq) || 0);
    }
    return mx;
  },
  /* [对话界面商业化 2026-10-17] 空态内容注入：宿主按卡类型给标题/说明/建议，
     视图层只管画（内容与位置分离，同 C2 的既有分工）。 */
  setEmpty(wid, cid, payload) {
    const st = sessions.get(skey(wid, cid));
    if (st && st.view && typeof st.view.setEmpty === "function") st.view.setEmpty(payload);
  },
  /* [5715 · 修] 收首屏骨架：宿主在「确定没有数据」时调用。
     病根：骨架只在 markLoaded() 后收起，而 markLoaded 只在 replayTrace 末尾调
     ⇒ 草稿路径（/work/draft、新建卡）不走 replayTrace ⇒ **骨架永远不收**，
     空态也被 loading 判据挡着 ⇒ 新用户第一屏是"永远在加载"。 */
  markLoaded(wid, cid) {
    const st = sessions.get(skey(wid, cid));
    if (st && st.view && typeof st.view.markLoaded === "function") st.view.markLoaded();
  },
};
