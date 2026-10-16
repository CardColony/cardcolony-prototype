/* esm/rule_panel.js —— 规则面板（规则卡六分区：直改即存）。
   [阶段1 A1.1 2026-10-17] 自 static/workspace.js 逐字搬运，零行为变更。
   面板 DOM：#rulePanelBody（templates/work.html 由本模块动态填充）。
   导出：loadRulePanel / renderRulePanel / saveRuleSection / getRuleCardId。 */
import { API } from "./api.js?v=35";
import { escapeHtml } from "./util.js?v=30";

const WORK_ID = window.__WORK_ID__;
const rulePanelBody = document.getElementById("rulePanelBody");

let ruleData = null;          // GET /rule 响应（含 sections/hashes/inject_type_counts）
let ruleCardId = "";          // 规则卡 card_id

    async function loadRulePanel() {
      if (!rulePanelBody) return;
      rulePanelBody.innerHTML = `<div class="rule-sec-status">加载中…</div>`;
      try {
        ruleData = await API.call("GET", `/api/works/${WORK_ID}/rule`);
        ruleCardId = (ruleData && ruleData.card_id) || "";
        renderRulePanel();
      } catch (e) {
        rulePanelBody.innerHTML = `<div class="rule-sec-status err">规则卡读取失败：${escapeHtml(e.message || "")}</div>`;
      }
    }

    function renderRulePanel() {
      if (!rulePanelBody || !ruleData) return;
      if (!ruleData.exists) {
        rulePanelBody.innerHTML =
          `<div class="rule-sec-status">本剧本还没有规则卡。<br>` +
          `新建一张「规则卡」（card_type=rule）即可启用。</div>`;
        return;
      }
      rulePanelBody.innerHTML = "";
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
        const r = await API.call("PATCH", `/api/works/${WORK_ID}/rule/sections/${encodeURIComponent(sec)}`,
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

/** 供根卡审批弹窗（仍在 workspace.js）兜底读规则卡 id。 */
export function getRuleCardId() { return ruleCardId; }

export { loadRulePanel, renderRulePanel, saveRuleSection };
