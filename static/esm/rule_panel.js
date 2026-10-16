/* esm/rule_panel.js —— [分页 2026-10-16] 规则卡（吸附右侧的常驻卡）——两页共用。
 *
 * 来源：原 workspace.js 的规则面板段整体抽出。规则卡「不分画布态/聊天态、始终在场」
 * （用户 2026-10-16 二次归位令），画布/对话拆页后它两页都在，故独立成模块。
 *
 * 正文按 ## {区名} 分区存 mission 列。直改即存：textarea blur → PATCH
 * /rule/sections/{section}（带 base_hash，不符→409 提示刷新）。
 * 审批（仅根卡提议修改）：走 timeline 扫描 draft_rule_edit tool_result → 弹审批窗
 * （「同意」→ POST /rule/commit 落盘）——审批窗复用对话页的 #draftModal DOM，
 * 画布页无该 DOM，withApproval=false 时整段不绑定（画布页也无回合收口触发点，天然不达）。 */
import { API } from "./api.js?v=35";
import { escapeHtml } from "./util.js?v=30";

export function initRulePanel(deps) {
  const workId = deps.workId || "";
  const api = deps.api || API;
  const withApproval = deps.withApproval === true;
  const rulePanel = document.getElementById("rulePanel");
  const rulePanelBody = document.getElementById("rulePanelBody");
  if (!rulePanelBody) return { maybeOpenRuleEditModal: null };

  let ruleData = null;          // GET /rule 响应（含 sections/hashes/inject_type_counts）
  let ruleCardId = "";          // 规则卡 card_id
  const ruleSeen = new Set();   // 已弹过审批的 draft_rule_edit 去重（tool_result 的 seq）

  async function loadRulePanel() {
    rulePanelBody.innerHTML = `<div class="rule-sec-status">加载中…</div>`;
    try {
      ruleData = await api.call("GET", `/api/works/${workId}/rule`);
      ruleCardId = (ruleData && ruleData.card_id) || "";
      renderRulePanel();
    } catch (e) {
      rulePanelBody.innerHTML = `<div class="rule-sec-status err">规则卡读取失败：${escapeHtml(e.message || "")}</div>`;
    }
  }

  function renderRulePanel() {
    rulePanelBody.innerHTML = "";
    if (!ruleData.exists) {
      rulePanelBody.innerHTML =
        `<div class="rule-sec-status">本剧本还没有规则卡。<br>` +
        `新建一张「规则卡」（card_type=rule）即可启用。</div>`;
      return;
    }
    const sections = ruleData.sections || {};
    const counts = ruleData.inject_type_counts || {};
    for (const [name, content] of Object.entries(sections)) {
      const wrap = document.createElement("div");
      wrap.className = "rule-sec";
      const n = counts[name] || 0;
      wrap.innerHTML =
        `<div class="rule-sec-head">` +
        `<span class="rule-sec-name">${escapeHtml(name)}</span>` +
        `<span class="rule-sec-inject">正被 ${n} 类卡注入</span>` +
        `</div>` +
        `<textarea class="rule-sec-textarea" data-sec="${escapeHtml(name)}">${escapeHtml(content || "")}</textarea>` +
        `<div class="rule-sec-status"></div>`;
      rulePanelBody.appendChild(wrap);
    }
    // blur 即存（无确认框）；status 行显示「已保存」或错误
    rulePanelBody.querySelectorAll(".rule-sec-textarea").forEach((ta) => {
      ta.addEventListener("blur", () => saveRuleSection(ta));
    });
  }

  async function saveRuleSection(ta) {
    const sec = ta.dataset.sec;
    const statusEl = ta.parentElement.querySelector(".rule-sec-status");
    const content = ta.value;
    const baseHash = (ruleData && ruleData.hashes && ruleData.hashes[sec]) || "";
    if (!ruleCardId) { if (statusEl) { statusEl.textContent = "规则卡不存在"; statusEl.classList.add("err"); } return; }
    if (statusEl) { statusEl.textContent = "保存中…"; statusEl.classList.remove("err"); }
    try {
      const r = await api.call("PATCH", `/api/works/${workId}/rule/sections/${encodeURIComponent(sec)}`,
        { content, base_hash: baseHash });
      if (ruleData && ruleData.hashes) ruleData.hashes[sec] = r.new_hash;
      if (ruleData && ruleData.sections) ruleData.sections[sec] = content;
      if (statusEl) statusEl.textContent = "已保存";
    } catch (e) {
      const msg = String(e.message || "");
      if (statusEl) {
        statusEl.textContent = msg.indexOf("hash 不符") >= 0 ? "内容已被改，请刷新重试" : ("保存失败：" + msg);
        statusEl.classList.add("err");
      }
    }
  }

  // 常驻卡：页面加载即渲染（不分画布/对话，始终吸附右侧）
  loadRulePanel();

  // ─── 根卡提议审批弹窗：扫 timeline 的 draft_rule_edit 成功 tool_result ───
  // 复用对话页 #draftModal 的 DOM，独立 dmRule 状态机（与块3起草弹窗互不干扰）。
  const dmRuleModal = withApproval ? document.getElementById("draftModal") : null;
  const dmRuleOverlay = withApproval ? document.getElementById("draftOverlay") : null;
  const dmRuleTitle = withApproval ? document.getElementById("draftModalTitle") : null;
  const dmRuleArea = withApproval ? document.getElementById("draftArea") : null;
  const dmRuleFbBox = withApproval ? document.getElementById("draftFeedbackBox") : null;
  const dmRuleFbInput = withApproval ? document.getElementById("draftFeedbackInput") : null;
  const dmRuleErr = withApproval ? document.getElementById("draftModalError") : null;
  const dmRuleConfirm = withApproval ? document.getElementById("draftConfirmBtn") : null;
  const dmRuleFeedback = withApproval ? document.getElementById("draftFeedbackBtn") : null;
  const dmRuleClose = withApproval ? document.getElementById("draftCloseBtn") : null;
  const dmRuleX = withApproval ? document.getElementById("draftModalX") : null;
  let dmRule = null;   // { section, old, draft, baseHash, ruleCardId, busy }

  function setRuleModalError(msg) {
    if (!dmRuleErr) return;
    if (msg) { dmRuleErr.textContent = msg; dmRuleErr.style.display = "block"; }
    else { dmRuleErr.textContent = ""; dmRuleErr.style.display = "none"; }
  }

  function openRuleEditModal(payload) {
    dmRule = {
      section: payload.section || "",
      old: payload.old || "",
      draft: payload.draft || "",
      baseHash: payload.base_hash || "",
      ruleCardId: payload.rule_card_id || ruleCardId,
      busy: false,
    };
    dmRuleTitle.textContent = `规则修改审批 · ${dmRule.section}`;
    dmRuleArea.innerHTML =
      `<div class="rule-old-block"><div class="rule-sec-name">当前内容</div><pre>${escapeHtml(dmRule.old)}</pre></div>` +
      `<div class="rule-new-block"><div class="rule-sec-name">提议修改为</div><pre>${escapeHtml(dmRule.draft)}</pre></div>`;
    dmRuleArea.classList.remove("loading");
    dmRuleFbBox.classList.remove("open");
    dmRuleFbInput.value = "";
    setRuleModalError("");
    dmRuleConfirm.disabled = false;
    dmRuleFeedback.disabled = true;   // 审批无「反馈重起草」，只留 同意/拒绝
    dmRuleFeedback.style.display = "none";
    dmRuleModal.classList.add("open");
  }

  async function submitRuleCommit() {
    if (!dmRule || dmRule.busy) return;
    dmRule.busy = true;
    dmRuleConfirm.disabled = true;
    try {
      await api.call("POST", `/api/works/${workId}/rule/commit`, {
        section: dmRule.section,
        content: dmRule.draft,
        base_hash: dmRule.baseHash,
      });
      api.toast("规则已写入", "success");
      closeRuleModal();
      loadRulePanel();   // 吸附卡常驻，落盘后即时重渲分区
    } catch (e) {
      const msg = String(e.message || "");
      setRuleModalError(msg.indexOf("hash 不符") >= 0 ? "内容已被改，请重新提议" : ("写入失败：" + msg));
      dmRuleConfirm.disabled = false;
    } finally {
      dmRule.busy = false;
    }
  }

  function closeRuleModal() {
    dmRule = null;
    dmRuleModal.classList.remove("open");
    dmRuleArea.textContent = "";
    dmRuleFbBox.classList.remove("open");
    setRuleModalError("");
    if (dmRuleFeedback) dmRuleFeedback.style.display = "";
  }

  if (dmRuleConfirm) dmRuleConfirm.onclick = () => { if (dmRule) submitRuleCommit(); };
  if (dmRuleClose) dmRuleClose.onclick = closeRuleModal;
  if (dmRuleX) dmRuleX.onclick = closeRuleModal;
  if (dmRuleOverlay) dmRuleOverlay.onclick = closeRuleModal;

  // 回合收口（send/resend/tryRecover 的 resync 之后）扫 timeline 开审批弹窗——对话页专属
  async function maybeOpenRuleEditModal(cardId) {
    if (!withApproval || !dmRuleModal) return;
    if (dmRule) return;   // 已有审批弹窗在，不叠开
    try {
      const tl = await api.getTimeline(workId, cardId);
      const items = (tl && tl.items) || [];
      for (let i = items.length - 1; i >= 0; i--) {
        const it = items[i];
        if (!it || it.type !== "tool_result") continue;
        const d = it.data || {};
        if (d.tool !== "draft_rule_edit" || !d.ok) continue;
        const seq = it.seq || 0;
        if (ruleSeen.has(seq)) continue;
        ruleSeen.add(seq);
        const res = (d.result && d.result.data) || {};
        if (res.section !== undefined) openRuleEditModal(res);
        return;   // 每次收口最多开一个
      }
    } catch (e) { /* 零阻断 */ }
  }

  return { maybeOpenRuleEditModal: withApproval ? maybeOpenRuleEditModal : null };
}
