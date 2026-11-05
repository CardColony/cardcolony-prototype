/* static/esm/exec_backend.js —— worker 卡的**执行体选择**块（纯函数，零 DOM）
 *
 * [wb_exec 2026-10-23] 从 workspace.js 抽出来：5715 那轮的纪律
 * 「锁在渲染闭包里的东西不可单测」——抽成独立模块才有测试缝。
 *
 * 渲染契约：
 *   · 未开工 ⇒ 状态行显示「未开工 · 开工后这里显示实际路由到的模型」，
 *     下拉选中项＝设置值（缺省 auto）。
 *   · 开工后 ⇒ 状态行显示**实际路由到的**（友好标签：版本名 · 模型）；
 *     下拉仍显示**你设的**值——「设置」与「实际」分开显示，不混。
 *   · 任何时候都能改下拉；改完下一回合生效（在跑的回合不打断）。
 */
import { escapeHtml } from "./util.js";

/** 执行体下拉的候选（后端给的形状）：
 *  {value, label, executor?, model?} */
export function buildExecBackendOptions(info) {
  const opts = info && Array.isArray(info.options) ? info.options : [];
  return opts.map(o => {
    const value = String(o && o.value != null ? o.value : "");
    const label = String(o && o.label != null ? o.label : value);
    const selected = value && value === String(info && info.backend || "") ? " selected" : "";
    return `<option value="${escapeHtml(value)}"${selected}>${escapeHtml(label)}</option>`;
  }).join("");
}

/** 「实际路由到的」的友好标签（用候选里的 label，别把 executor id 甩给人看）。 */
export function routedLabel(info) {
  const r = info && info.routed;
  if (!r) return "";
  // ★[回落 2026-10-23 19:55] 委派失败**静默回落**系统模型时，卡面必须**如实显示**，
  //   不能还显示成"走 WorkBuddy"（那是假信号 ⇒ 人会以为在 WorkBuddy 上跑）。
  if (r.fallback === "system") {
    const why = String(r.reason || "执行体不可用");
    return `系统自己的模型（${why}）`;
  }
  const ex = String(r.executor || "");
  const md = String(r.model || "");
  // **两个都要**：缺一个就是残缺记录 ⇒ 当作"没路由到"（不显示「本次路由」），
  // 否则会渲染出 "undefined · hy3" 这种脏字样。
  if (!ex || !md) return "";
  const hit = ((info && info.options) || []).find(
    o => o && o.executor === ex && o.model === md);
  return hit && hit.label ? String(hit.label) : `${ex} · ${md}`;
}

/** 渲染执行体块（返回 HTML 字符串）。
 *
 *  [总开关 2026-10-23 16:20] `sw` ＝ **全局手动开关**（{enabled: bool}）：
 *    开 ⇒ 走 WorkBuddy；关 ⇒ 全部走系统自己的模型（此时下拉置灰）。
 *    开关是**全局**的（写在 config/wb_exec_switch.json，运行时生效，不用重启）。
 */
export function buildExecBackendHtml(info, sw) {
  const opts = buildExecBackendOptions(info);
  const rl = routedLabel(info);
  const isFallback = !!(info && info.routed && info.routed.fallback === "system");
  const routed = rl
    ? `${isFallback ? "本轮实际" : "本次路由"} → ${escapeHtml(rl)}`
    : "未开工 · 开工后这里显示实际路由到的模型";
  const cfgOn = !!(info && info.enabled);
  const swOn = sw === undefined || sw === null
    ? cfgOn                      // 没给开关状态 ⇒ 退回配置态
    : !!(sw.enabled);
  const on = cfgOn && swOn;
  const off = !cfgOn ? "（执行体未启用：当前全部走系统自己的模型）"
            : !swOn ? "（总开关已关闭：当前全部走系统自己的模型）"
            : "";
  const swChecked = swOn ? " checked" : "";
  const swDisabled = cfgOn ? "" : " disabled";
  return `
      <div class="blocktitle">执行体<span class="note">谁干活：外部执行体（Devin / opencode / WorkBuddy）还是系统自己的模型</span></div>
      <div class="exec-row">
        <label class="exec-switch">
          <input type="checkbox" id="execBackendSwitch"${swChecked}${swDisabled}>
          <span>走外部执行体${swOn ? "" : "（已关闭）"}</span>
        </label>
      </div>
      <div class="exec-row">
        <select id="execBackendSel" class="exec-sel"${on ? "" : " disabled"}>${opts}</select>
      </div>
      <div class="exec-status" id="execBackendStatus">${routed}</div>
      <div class="exec-hint">${off}切换在下一回合生效（在跑的回合不打断）。</div>
    `;
}
