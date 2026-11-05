/* esm/rule_panel.js —— 规则面板（项目规则分区：直改即存）。
   [阶段1 A1.1 2026-10-17] 自 static/workspace.js 逐字搬运，零行为变更。
   [规则节点 2026-10-28 · 用户令「一个项目一个文件」] 规则不再是一张卡：
   它是项目文件夹里的 rules.md（``works/<work_id>/rules.md``），所以面板不再
   持有 ruleCardId，也不再为了保存而先建一张 card_type=rule 的卡。
   面板 DOM：#rulePanelBody（templates/work.html 由本模块动态填充）。
   导出：loadRulePanel / renderRulePanel / saveRuleSection / getRuleCardId。 */
import { API } from "./api.js?v=55";
import { escapeHtml } from "./util.js?v=30";

/* [惰性新建 2026-10-17] WORK_ID 改惰性读：草稿落库成功后 window.__WORK_ID__ 就地更新
 * （画布页 canvas.js / 对话页 workspace.js 写），本模块须随取随新——原 const 捕获在
 * 落库后会失准，规则口会继续打草稿 id（404）。 */
const WID = () => window.__WORK_ID__;
const isDraft = () => window.__WORK_ID__ === "draft";
const rulePanelBody = document.getElementById("rulePanelBody");

/* [惰性新建 2026-10-17] 草稿态分区名镜像：后端 context_regions.rule_section_names()
 * （总规则 + 词表 codes，root 在前）。草稿项目尚未落库 ⇒ 无 /rule 可读，用本镜像
 * 渲染空区；保存时先走落库序列（建项目——规则文件由 create_work 一起建出来），
 * 再把内容写进真身。
 * ★ 本镜像必须与后端一致，tests/test_rule_card.py 有一条断言比对两边。 */
const DRAFT_RULE_SECTIONS = ["all", "project", "function", "sub_project",
                             "tool", "worker", "ui", "qa_card", "pm_card",
                             "rule", "review", "topic"];

/* 分区显示名（与后端 rule_store.SECTION_LABELS 同口径；缺失按 code 原样显示）。 */
const SECTION_LABELS = {
  all: "总规则（所有卡）", project: "根卡", sub_project: "子项目卡",
  tool: "工具卡", worker: "worker 卡", ui: "UI卡", qa_card: "验收卡", pm_card: "执笔卡",
  function: "全体职能",
  review: "评审卡", rule: "规则节点自身", topic: "课题卡",
};
const labelOf = (n) => SECTION_LABELS[n] || n;

let ruleData = null;          // GET /rule 响应（含 sections/hashes/inject_type_counts）
/* [规则节点 2026-10-28] 不再有 ruleCardId：规则是文件不是卡。 */

    async function loadRulePanel() {
      if (!rulePanelBody) return;
      // [D6] 草稿态：不查库（GET /rule 对 draft → 404）——渲染六个空区，可编辑，
      // 保存即触发落库序列（见 saveRuleSection）。
      if (isDraft()) { renderDraftRulePanel(); return; }
      rulePanelBody.innerHTML = `<div class="rule-sec-status">加载中…</div>`;
      try {
        ruleData = await API.call("GET", `/api/works/${WID()}/rule`);
        renderRulePanel();
      } catch (e) {
        rulePanelBody.innerHTML = `<div class="rule-sec-status err">规则读取失败：${escapeHtml(e.message || "")}</div>`;
      }
    }

    /** [惰性新建 2026-10-17] 草稿项目态规则面板：六区空文本域（不查库）。 */
    function renderDraftRulePanel() {
      ruleData = null;
      rulePanelBody.innerHTML = "";
      const tip = document.createElement("div");
      tip.className = "rule-sec-status";
      tip.textContent = "新项目（未保存）——编辑任一区并保存，即创建项目并把规则写进去。";
      rulePanelBody.appendChild(tip);
      for (const name of DRAFT_RULE_SECTIONS) {
        const wrap = document.createElement("div");
        wrap.className = "rule-sec";
        wrap.innerHTML =
          `<div class="rule-sec-head"><span class="rule-sec-name">${escapeHtml(labelOf(name))}</span>` +
          `<span class="rule-sec-code">${escapeHtml(name)}</span></div>` +
          `<textarea class="rule-sec-textarea" data-sec="${escapeHtml(name)}" data-draft="1"></textarea>` +
          `<div class="rule-sec-status"></div>`;
        rulePanelBody.appendChild(wrap);
      }
      rulePanelBody.querySelectorAll(".rule-sec-textarea").forEach((ta) => {
        ta.addEventListener("blur", () => saveRuleSection(ta));
      });
    }

    function renderRulePanel() {
      if (!rulePanelBody || !ruleData) return;
      if (!ruleData.exists) {
        // 规则随项目自带（create_work 建 rules.md），正常不会走到这里；
        // 真走到说明文件被外部删了，提示刷新即由后端补建。
        rulePanelBody.innerHTML =
          `<div class="rule-sec-status">规则文件缺失，刷新后会自动补建。</div>`;
        return;
      }
      rulePanelBody.innerHTML = "";
      const sections = ruleData.sections || {};
      const counts = ruleData.inject_type_counts || {};
      for (const [name, content] of Object.entries(sections)) {
        const wrap = document.createElement("div");
        wrap.className = "rule-sec";
        const n = counts[name] || 0;
        const inject = (name === "all") ? "进所有卡" : `进 ${n} 类卡`;
        wrap.innerHTML =
          `<div class="rule-sec-head">` +
          `<span class="rule-sec-name">${escapeHtml(labelOf(name))}</span>` +
          `<span class="rule-sec-code">${escapeHtml(name)}</span>` +
          `<span class="rule-sec-inject">${escapeHtml(inject)}</span>` +
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
      // [惰性新建 2026-10-17 · D2/D3] 草稿态：保存本身即数据动作 ⇒ 先走落库序列
      // （建项目 + 建规则卡），再把内容写进真身。失败：报错、保持草稿态、内容不丢、可重试。
      if (isDraft()) {
        if (statusEl) { statusEl.textContent = "保存中（创建项目）…"; statusEl.classList.remove("err"); }
        try {
          await window.__draftEnsurePersisted?.();
        } catch (e) {
          if (statusEl) { statusEl.textContent = "创建项目失败：" + String((e && e.message) || e); statusEl.classList.add("err"); }
          return;   // 保持草稿态：textarea 内容原样保留，可重试
        }
        if (isDraft()) {   // 无落库钩子（异常路径）：不硬打请求，避免 404
          if (statusEl) { statusEl.textContent = "项目尚未保存，请重试"; statusEl.classList.add("err"); }
          return;
        }
        // [规则节点 2026-10-28] 落库后规则文件已由 create_work 建好
        // （works/<w>/rules.md）——不需要再建任何卡，直接读真面板拿真 hash。
        await loadRulePanel();   // 读真面板拿各区真 hash
        const ta2 = rulePanelBody.querySelector(`.rule-sec-textarea[data-sec="${sec}"]`);
        if (!ta2) { if (statusEl) { statusEl.textContent = "保存失败（区不存在）"; statusEl.classList.add("err"); } return; }
        ta2.value = content;
        await saveRuleSection(ta2);   // 走真路径（此时 isDraft() 已 false）
        return;
      }
      const baseHash = (ruleData && ruleData.hashes && ruleData.hashes[sec]) || "";
      if (statusEl) { statusEl.textContent = "保存中…"; statusEl.classList.remove("err"); }
      try {
        const r = await API.call("PATCH", `/api/works/${WID()}/rule/sections/${encodeURIComponent(sec)}`,
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

/** [规则节点 2026-10-28] 规则是文件不是卡，恒返回空串。
 *  保留导出只为不打断 workspace.js 的审批弹窗（它已不再需要这个 id）。 */
export function getRuleCardId() { return ""; }

export { loadRulePanel, renderRulePanel, saveRuleSection };
