/* workspace.js - 卡片工作区（左35%资料 + 右65%讨论区） */
(function () {
  "use strict";

  const WORK_ID = window.__WORK_ID__;
  const ws = document.getElementById("workspace");
  const wsLeft = document.getElementById("wsLeft");
  const chatArea = document.getElementById("chatArea");
  const inputBox = document.getElementById("inputBox");
  const sendBtn = document.getElementById("sendBtn");

  let currentCard = null;     // 完整 card 数据 {meta, body}
  let lastSentContent = "";   // 用于失败重发（不重复追加用户消息）

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  function formatMsgContent(text) {
    // 换行转 <br>，```代码块``` 转 <pre>
    const parts = text.split(/```/);
    let html = "";
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {
        // 代码块
        html += `<pre>${escapeHtml(parts[i])}</pre>`;
      } else {
        html += escapeHtml(parts[i]).replace(/\n/g, "<br>");
      }
    }
    return html;
  }

  async function open(cardId) {
    try {
      const card = await API.getCard(WORK_ID, cardId);
      currentCard = card;
      await renderLeft();
      await renderChat();
      // [刷新恢复 2026-10-03] 卡号写进 URL hash：刷新后按 hash 自动重开这张卡。
      // 用 replaceState 不堆积浏览历史、不触发页面重载；URL 即状态，不引入前端存储。
      if (location.hash !== "#" + cardId) {
        history.replaceState(null, "", "#" + cardId);
      }
      ws.classList.add("active");
      // [phase8.4.1] display:none 期间 scrollHeight=0，renderChat 里的滚底是空操作；
      // 必须在面板激活（可见、完成排版）之后再锁定到最新消息
      scrollChatBottom();
      // [phase8.6-C] 同理：textarea auto-grow 必须在面板可见后执行
      autoGrowAll();
    } catch (e) { /* toast 已处理 */ }
  }
  window.Workspace = { open };

  async function renderLeft() {
    const m = currentCard.meta;
    const b = currentCard.body;

    const relations = m.relations || [];
    // [phase8.6-E] 每条关系尾部 ✕ 删除按钮（data-idx 定位）
    const relationsHtml = relations.length === 0
      ? `<div class="rej-empty">（无关系）</div>`
      : relations.map((r, i) => `<div class="rel-item" data-target="${escapeHtml(r.target || "")}">
            <span class="rel-type">${escapeHtml(r.type || "")} →</span>
            <span>${escapeHtml(r.target || "")}</span>
            <button class="rel-del" data-idx="${i}" title="删除该关系">✕</button>
          </div>`).join("");

    // [phase8.6-E] 添加关系行：目标卡下拉（画布卡集，排除自身）+ 类型下拉
    const canvasCards = (window.GetCanvasState ? (window.GetCanvasState().cards || []) : [])
      .filter(c => c.card_id !== m.card_id);
    const targetOptions = canvasCards
      .map(c => `<option value="${escapeHtml(c.card_id)}">${escapeHtml(c.card_id)} · ${escapeHtml(c.title)}</option>`)
      .join("");
    const relAddHtml = `
      <div class="rel-add-row">
        <select id="relTargetSel"><option value="">＋ 添加关系：选目标卡</option>${targetOptions}</select>
        <select id="relTypeSel">
          <option value="先后">先后</option>
          <option value="锚定">锚定</option>
          <option value="兑现">兑现</option>
          <option value="关联">关联</option>
        </select>
        <button class="btn" id="relAddBtn">确定</button>
      </div>`;

    const rejections = (b.rejections || "").trim();
    const rejHtml = rejections
      ? rejections.split("\n").filter(s => s.trim()).map(line => `<div class="rej-item">${escapeHtml(line)}</div>`).join("")
      : `<div class="rej-empty">（暂无否决）</div>`;

    const history = b.history || [];
    const historyCount = history.length;

    wsLeft.innerHTML = `
      <button class="ws-back" id="wsBackBtn" title="返回画布（重拉画布数据）">← 返回画布</button>
      <div class="ws-title" id="wsTitle">${escapeHtml(m.title)}</div>
      <div style="margin:8px 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span class="type-badge" data-type="${escapeHtml(m.card_type)}">${escapeHtml(m.card_type)}卡</span>
        <span style="font-size:11px;color:var(--dimmer);margin-left:auto;">${escapeHtml(m.card_id)} · ${escapeHtml(m.source)}</span>
      </div>

      <div class="blocktitle">职责区 · charter <span class="note">由上级卡对话创建 · 可编辑</span></div>
      <textarea class="ws-edit" id="charterEdit" placeholder="使命 / 方法 / 权能">${escapeHtml(b.charter || "")}</textarea>
      <div class="edit-row">
        <button class="btn" id="saveCharter">保存职责区</button>
        <span class="save-status" id="charterStatus"></span>
      </div>

      <div class="blocktitle">结论区</div>
      <textarea class="ws-edit" id="conclusionEdit" placeholder="本卡的结论（第一条建议是一句话摘要）">${escapeHtml(b.conclusion || "")}</textarea>
      <div class="edit-row">
        <button class="btn" id="saveConclusion">保存结论</button>
        <span class="save-status" id="conclusionStatus"></span>
      </div>

      <div class="blocktitle">否决记录</div>
      ${rejHtml}

      <div class="blocktitle">关系</div>
      ${relationsHtml}
      ${relAddHtml}

      <div class="blocktitle">修改历史（${historyCount} 条）</div>
      <div class="history-block">
        <div class="history-toggle" id="historyToggle">▸ 展开/折叠</div>
        <div class="history-list" id="historyList">
          ${history.slice().reverse().map(h => `<div class="h-item">${escapeHtml(h)}</div>`).join("")}
        </div>
      </div>

      <div class="blocktitle">＋ 建子卡</div>
      <div class="add-child-row">
        <input type="text" id="childTitle" placeholder="子卡标题">
        <!-- D-06（T07）：只关新建入口、不删白名单 —— 四个废弃类型名不再提供；ALLOWED_CARD_TYPE 六值元组原样保留 -->
        <select id="childType">
          <option value="线索">线索</option>
        </select>
        <button class="btn" id="addChildBtn">建</button>
      </div>

      ${m.card_type !== "剧本" ? `
      <div class="blocktitle" style="color:#d05656;">危险操作</div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:24px;flex-wrap:wrap;">
        <button class="btn" id="deleteCardBtn" style="color:#d05656;border-color:#d05656;">删除此卡</button>
        <span style="font-size:11px;color:var(--dimmer);">连同会话删除；有子卡时系统拒绝；剧本卡无此操作</span>
      </div>` : ""}
    `;

    bindLeftEvents();
    // [phase8.6-C] 面板已可见时（如保存后重渲染）立即撑高；首次 open 由 active 后统一触发
    autoGrowAll();
  }

  // [phase8.6-C] 文本区高度自适应：内容多高就多高，上限由 CSS max-height 兜底
  function autoGrowEl(el) {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }
  function autoGrowAll() {
    autoGrowEl(document.getElementById("charterEdit"));
    autoGrowEl(document.getElementById("conclusionEdit"));
  }

  function bindLeftEvents() {
    // [phase8.6-C] 输入时实时撑高
    ["charterEdit", "conclusionEdit"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", () => autoGrowEl(el));
    });

    // 返回画布（必须用 ReloadCanvas：对话中可能生了新卡，需要重拉画布数据）
    document.getElementById("wsBackBtn").onclick = () => window.ReloadCanvas();

    // 标题编辑
    const titleEl = document.getElementById("wsTitle");
    titleEl.onclick = () => {
      const input = document.createElement("input");
      input.type = "text";
      input.className = "ws-title-edit";
      input.value = currentCard.meta.title;
      titleEl.replaceWith(input);
      input.focus();
      input.select();
      const save = async () => {
        const v = input.value.trim();
        if (!v) { API.toast("标题不能为空", "error"); return; }
        if (v === currentCard.meta.title) { input.replaceWith(titleEl); return; }
        try {
          const r = await API.patchCard(WORK_ID, currentCard.meta.card_id, { title: v }, "用户修改标题");
          currentCard = r;
          await renderLeft();
        } catch (e) { /* toast 已处理 */ }
      };
      input.onblur = save;
      input.onkeydown = (e) => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        if (e.key === "Escape") { input.value = currentCard.meta.title; input.blur(); }
      };
    };

    // charter 保存
    document.getElementById("saveCharter").onclick = async () => {
      const v = document.getElementById("charterEdit").value;
      const status = document.getElementById("charterStatus");
      status.textContent = "保存中…";
      try {
        const r = await API.patchCard(WORK_ID, currentCard.meta.card_id, { charter: v }, "用户编辑职责区");
        currentCard = r;
        status.textContent = "已保存";
        setTimeout(() => status.textContent = "", 2000);
      } catch (e) { status.textContent = "失败"; }
    };

    // conclusion 保存
    document.getElementById("saveConclusion").onclick = async () => {
      const v = document.getElementById("conclusionEdit").value;
      const status = document.getElementById("conclusionStatus");
      status.textContent = "保存中…";
      try {
        const r = await API.patchCard(WORK_ID, currentCard.meta.card_id, { conclusion: v }, "用户编辑结论区");
        currentCard = r;
        status.textContent = "已保存";
        setTimeout(() => status.textContent = "", 2000);
      } catch (e) { status.textContent = "失败"; }
    };

    // 关系项跳转
    wsLeft.querySelectorAll(".rel-item").forEach(item => {
      item.onclick = () => {
        const target = item.dataset.target;
        if (target) open(target);
      };
    });

    // [phase8.6-E] 删除关系：PATCH 删除该条后的完整数组，成功后就地重渲染
    wsLeft.querySelectorAll(".rel-del").forEach(btn => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        const idx = parseInt(btn.dataset.idx, 10);
        const rels = (currentCard.meta.relations || []).slice();
        if (isNaN(idx) || idx < 0 || idx >= rels.length) return;
        const removed = rels.splice(idx, 1)[0];
        try {
          const r = await API.patchCard(WORK_ID, currentCard.meta.card_id,
            { relations: rels }, `用户删除关系：${removed.type || ""} → ${removed.target || ""}`);
          currentCard = r;
          await renderLeft();
        } catch (e2) { /* toast 已处理 */ }
      };
    });

    // [phase8.6-E] 添加关系：目标 + 类型 → PATCH 追加，成功后就地重渲染
    const relAddBtn = document.getElementById("relAddBtn");
    if (relAddBtn) {
      relAddBtn.onclick = async () => {
        const target = document.getElementById("relTargetSel").value;
        const type = document.getElementById("relTypeSel").value;
        if (!target) { API.toast("请选择目标卡", "error"); return; }
        const rels = (currentCard.meta.relations || []).slice();
        if (rels.some(r => r.target === target && r.type === type)) {
          API.toast("该关系已存在", "error"); return;
        }
        rels.push({ target, type });
        try {
          const r = await API.patchCard(WORK_ID, currentCard.meta.card_id,
            { relations: rels }, `用户添加关系：${type} → ${target}`);
          currentCard = r;
          await renderLeft();
        } catch (e2) { /* toast 已处理 */ }
      };
    }

    // 历史折叠
    document.getElementById("historyToggle").onclick = () => {
      const list = document.getElementById("historyList");
      list.classList.toggle("expanded");
    };

    // 建子卡
    document.getElementById("addChildBtn").onclick = async () => {
      const title = document.getElementById("childTitle").value.trim();
      if (!title) { API.toast("请输入子卡标题", "error"); return; }
      const ctype = document.getElementById("childType").value;
      try {
        await API.createCard(WORK_ID, {
          title, source: "用户直接创建",
          card_type: ctype, parent: currentCard.meta.card_id,
        });
        document.getElementById("childTitle").value = "";
        API.toast("已建子卡：" + title, "success");
      } catch (e) { /* toast 已处理 */ }
    };
    document.getElementById("childTitle").onkeydown = (e) => {
      if (e.key === "Enter") document.getElementById("addChildBtn").click();
    };

    // [用户删卡 2026-10-02] prompt 输入标题前三字放行；成功后返回画布并重拉
    const delBtn = document.getElementById("deleteCardBtn");
    if (delBtn) {
      delBtn.onclick = async () => {
        const title = currentCard.meta.title || "";
        const head = title.slice(0, 3);
        if (!head) { API.toast("标题为空，无法校验", "error"); return; }
        const input = window.prompt(
          `删除此卡不可撤销（连同会话；git 可回溯）。\n请输入标题前三个字放行：「${head}」`
        );
        if (input === null) return;
        if (input.trim() !== head) { API.toast("校验不符，未删除", "error"); return; }
        const cid = currentCard.meta.card_id;
        try {
          await API.deleteCard(WORK_ID, cid);
          API.toast("已删除：" + title, "success");
          window.ReloadCanvas();
        } catch (e) { /* toast 已处理（后端 error 文本透传） */ }
      };
    }
  }

  // ==================== 讨论区 ====================
  async function renderChat() {
    chatArea.innerHTML = "";
    try {
      const msgs = await API.getSession(WORK_ID, currentCard.meta.card_id);
      for (const m of msgs) appendMessage(m.role, m.content, m.model);
    } catch (e) { /* toast 已处理 */ }
    scrollChatBottom();
  }

  function appendMessage(role, content, model) {
    const el = document.createElement("div");
    el.className = "msg " + (role === "user" ? "user" : "assistant");
    const badge = (role === "assistant" && model) ? `<div class="model-badge">${escapeHtml(model)}</div>` : "";
    el.innerHTML = badge + formatMsgContent(content || "");
    chatArea.appendChild(el);
    scrollChatBottom();
    return el;
  }

  function scrollChatBottom() {
    chatArea.scrollTop = chatArea.scrollHeight;
  }

  // ==================== 发送消息 ====================

  // [phase8.2] 等待反馈气泡：轮询后端真实进度（三席分析→统合→工具→落卡），收到响应/失败后 stop() 清除
  // [实时反馈 2026-10-02] 后端暴露 /progress 阶段态；展示层专用，不影响模型输入
  const STAGE_TEXT = {
    started: "已提交，进入链路",
    viewpoints: "三席分析中",
    synthesize: "统合推理中",
    tools: "工具循环",
    cards: "解析建卡",
    done: "完成",
    error: "调用失败",
    idle: "等待开始",
  };
  function createWaitingBubble(prefix, cardIdAtSend) {
    const el = document.createElement("div");
    el.className = "msg thinking";
    let seconds = 0;
    let stageLine = "";
    const render = () => {
      el.innerHTML = `<span class="spinner"></span>${prefix} ${escapeHtml(stageLine || "…")} · 已等待 ${seconds} 秒`;
    };
    render();
    chatArea.appendChild(el);
    scrollChatBottom();
    const timer = setInterval(() => { seconds += 1; render(); }, 1000);
    const poll = setInterval(async () => {
      const cid = cardIdAtSend || (currentCard ? currentCard.meta.card_id : "");
      if (!cid) return;
      try {
        const st = await API.getProgress(WORK_ID, cid);
        if (!st || st.stage === "idle") return;
        const label = STAGE_TEXT[st.stage] || st.stage;
        stageLine = st.detail ? `${label}｜${st.detail}` : label;
        render();
      } catch (e) { /* 进度轮询失败静默：不影响主等待 */ }
    }, 2000);
    return {
      stop() {
        clearInterval(timer);
        clearInterval(poll);
        el.remove();
      },
    };
  }

  // [刷新恢复实时状态 2026-10-03] 页面加载时重新挂接在途调用的等待气泡。
  // 问题：等待气泡的 timer/poll/el 全在 createWaitingBubble 的闭包里，刷新即随 JS 上下文销毁；
  //       后端 progress 状态（模块级内存、key=(work_id,card_id)）与端点都还在，但加载路径从不查它，
  //       所以刷新后「正在跑」的实时状态消失（历史消息由 restoreFromHash→open→renderChat 恢复）。
  // 修法：加载时查一次后端 progress——活跃态则复用 createWaitingBubble 挂回气泡与轮询；
  //       done/error/idle 及任何异常一律静默降级为现有行为，绝不抛错打断页面。
  // 硬约束：纯前端改动，静态文件热生效，零重启。
  // 活跃态白名单显式列出——done/error 是终止态，若当活跃处理会让已完成的卡永远转圈。
  const ACTIVE_STAGES = ["started", "viewpoints", "synthesize", "tools", "cards"];
  async function attachRunningState(cardId) {
    if (!cardId) return false;
    try {
      const st = await API.getProgress(WORK_ID, cardId);
      if (!st || !ACTIVE_STAGES.includes(st.stage)) return false;
      // 同一张卡上已有等待气泡时不重复挂（避免 hashchange 等路径重复触发）
      if (chatArea.querySelector(".msg.thinking")) return false;
      createWaitingBubble("思考中…（刷新前已启动）", cardId);
      return true;
    } catch (e) {
      return false;   // 查询失败静默：不影响页面其余功能
    }
  }

  async function send() {
    if (!currentCard) return;
    const content = inputBox.value.trim();
    if (!content) return;

    // 立即上屏用户气泡
    appendMessage("user", content);
    lastSentContent = content;
    inputBox.value = "";
    inputBox.style.height = "auto";

    // [hotfix] 自愈基线：当前已上屏消息数（含刚上屏的用户气泡）＝服务端追加用户消息后的条数
    const baseline = chatArea.querySelectorAll(".msg:not(.thinking)").length;
    const cardId = currentCard.meta.card_id;
    let settled = false;

    const waiting = createWaitingBubble("思考中…", cardId);

    // [hotfix] 后端完成但连接断了 → 从会话记录恢复回复，杜绝无限转圈
    async function tryRecover() {
      if (settled) return false;
      try {
        const msgs = await API.getSession(WORK_ID, cardId);
        if (msgs.length >= baseline + 1 && msgs[msgs.length - 1].role === "assistant") {
          settled = true;
          waiting.stop();
          await renderChat();
          const note = document.createElement("div");
          note.className = "sys-tools";
          note.textContent = "⚠ 响应连接中断，已从会话记录恢复回复（若有新卡片，返回画布后可见）";
          chatArea.appendChild(note);
          scrollChatBottom();
          return true;
        }
      } catch (e2) { /* 静默，下轮再试 */ }
      return false;
    }
    const pollTimer = setInterval(tryRecover, 15000);

    sendBtn.disabled = true;

    try {
      const resp = await API.postMessage(WORK_ID, cardId, content);
      if (settled) return;   // 已由轮询恢复，丢弃迟到响应
      settled = true;
      waiting.stop();
      appendMessage("assistant", resp.reply, resp.model);

      // created_cards 绿条
      if (resp.created_cards && resp.created_cards.length > 0) {
        const card = document.createElement("div");
        card.className = "sys-newcards";
        const items = resp.created_cards.map(c =>
          `<span class="nc-item" data-cid="${escapeHtml(c.card_id)}">${escapeHtml(c.title)}</span>`
        ).join("");
        card.innerHTML = `<b>✚ 已创建 ${resp.created_cards.length} 张卡片</b>：${items}`;
        chatArea.appendChild(card);
        card.querySelectorAll(".nc-item").forEach(it => {
          it.onclick = () => open(it.dataset.cid);
        });
      }

      // tool_events 灰条
      if (resp.tool_events && resp.tool_events.length > 0) {
        const tool = document.createElement("div");
        tool.className = "sys-tools";
        const items = resp.tool_events.map(t =>
          `<span class="te-item ${t.ok ? "" : "fail"}">🔧 ${escapeHtml(t.tool)}(${escapeHtml(t.card_id)}) ${t.ok ? "✓" : "✗"}</span>`
        ).join("");
        tool.innerHTML = items;
        chatArea.appendChild(tool);
      }

      // [phase9-F] 观点元信息灰条（simple 多模型；有 viewpoints_meta 才显示）
      if (resp.viewpoints_meta && resp.viewpoints_meta.length > 0) {
        const vp = document.createElement("div");
        vp.className = "sys-tools";
        const items = resp.viewpoints_meta.map(v => {
          const secs = ((v.latency_ms || 0) / 1000).toFixed(1) + "s";
          const status = v.ok ? `✓ ${secs}` : `✗ ${escapeHtml(v.error || "失败").slice(0, 24)}`;
          return `<span class="te-item ${v.ok ? "" : "fail"}">${escapeHtml(v.model)} ${status}</span>`;
        }).join(" ｜ ");
        vp.innerHTML = `观点：${items}`;
        chatArea.appendChild(vp);
      }

      scrollChatBottom();
    } catch (e) {
      if (settled) return;
      // [hotfix] 报错前先探一次会话：后端可能已完成，只是响应没送回来
      if (await tryRecover()) return;
      waiting.stop();
      // 失败：红色系统条 + 重发按钮
      const err = document.createElement("div");
      err.className = "sys-error";
      err.innerHTML = `请求失败：${escapeHtml(e.message || "")} <button class="retry-btn">重发</button>`;
      chatArea.appendChild(err);
      err.querySelector(".retry-btn").onclick = () => {
        err.remove();
        resend(content);
      };
      scrollChatBottom();
    } finally {
      clearInterval(pollTimer);
      sendBtn.disabled = false;
    }
  }

  // 重发：不重复追加用户消息，直接重调接口
  async function resend(content) {
    const cardId = currentCard ? currentCard.meta.card_id : "";
    const waiting = createWaitingBubble("重发中…", cardId);
    sendBtn.disabled = true;
    try {
      const resp = await API.postMessage(WORK_ID, currentCard.meta.card_id, content);
      waiting.stop();
      appendMessage("assistant", resp.reply, resp.model);
      scrollChatBottom();
    } catch (e) {
      waiting.stop();
      const err = document.createElement("div");
      err.className = "sys-error";
      err.innerHTML = `重发失败：${escapeHtml(e.message || "")} <button class="retry-btn">再试</button>`;
      chatArea.appendChild(err);
      err.querySelector(".retry-btn").onclick = () => { err.remove(); resend(content); };
      scrollChatBottom();
    } finally {
      sendBtn.disabled = false;
    }
  }

  sendBtn.onclick = send;
  inputBox.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      send();
    }
  });
  // 输入框自适应高度
  inputBox.addEventListener("input", () => {
    inputBox.style.height = "auto";
    inputBox.style.height = Math.min(inputBox.scrollHeight, 200) + "px";
  });

  // ==================== [刷新恢复 2026-10-03] 加载时按 URL hash 重开上次的卡 ====================
  // 问题：页面上看的那张卡刷新即丢——回读历史的能力（open→renderChat→GET /session）三层齐备，
  // 但加载时没有任何代码去调 open()，所以刷新后对话区空白。
  // 修法：卡号存 URL hash（open() 内写入），加载时读出来重开。不引入 localStorage 等前端存储。
  async function restoreFromHash() {
    const hash = (location.hash || "").replace(/^#/, "");
    if (!hash) return false;
    try {
      await open(hash);
      // [刷新恢复实时状态 2026-10-03] 历史回读完成后，查一次后端进度：若这张卡有调用在跑，
      // 把等待气泡与 2 秒轮询挂回去——刷新不再丢失「正在思考」的实时状态。
      await attachRunningState(hash);
      return true;
    } catch (e) {
      // 卡片不存在（已是旧链接）等情形：open() 内部已 toast，这里回退到根卡，
      // 并把失效的 hash 清掉，避免每次刷新都撞同一个失败。
      history.replaceState(null, "", location.pathname + location.search);
      return false;
    }
  }

  // 浏览器后退/前进时同步切卡（hash 变化即重开）
  window.addEventListener("hashchange", () => {
    const hash = (location.hash || "").replace(/^#/, "");
    if (hash && (!currentCard || currentCard.meta.card_id !== hash)) {
      open(hash);
    }
  });

  restoreFromHash();

})();
