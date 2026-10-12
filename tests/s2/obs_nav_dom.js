/* tests/s2/obs_nav_dom.js —— [S2] DOM/网络观察脚本（V-R1~R4 / V-V4~V6 真机判据）。
 *
 * 用法（浏览器 console 或自动化注入，per work 页一次）：
 *   1. 强刷页面（Ctrl+F5）让 S2 新码生效；
 *   2. 注入本脚本（整体粘贴回车）——装 MutationObserver + fetch 计数；
 *   3. window.__navObs.reset() 清账；
 *   4. 手工/自动化执行操作序列（见底部「判据操作序列」）；
 *   5. copy(window.__navObs.report()) 取结果，或 console.table 查看。
 *
 * 只读观察：不改任何页面状态、不写 works；localStorage 仅在判据 V-V5 的
 * 「清偏好重载」步骤由操作者显式执行（localStorage.removeItem('nav.prefs.v1')）。
 */
(function () {
  if (window.__navObs) { console.warn("[navObs] 已注入，先 window.__navObs.destroy() 再重注"); return; }

  const tree = document.getElementById("navTree");
  const log = [];
  const netCounts = { children: 0, navFull: 0, version: 0 };
  const origFetch = window.fetch.bind(window);
  window.fetch = function (url, opts) {
    const u = String(url);
    if (u.includes("/api/nav/children")) netCounts.children++;
    else if (u.endsWith("/api/nav") || u.includes("/api/nav?")) netCounts.navFull++;
    else if (u.includes("/api/nav/version")) netCounts.version++;
    return origFetch(url, opts);
  };

  let recording = false;
  const mo = new MutationObserver((muts) => {
    if (!recording) return;
    for (const m of muts) {
      for (const n of m.addedNodes) {
        if (n.nodeType === 1) log.push({ t: "add", cls: n.className, cid: n.dataset && (n.dataset.cid || n.dataset.wid), txt: (n.textContent || "").slice(0, 40) });
      }
      for (const n of m.removedNodes) {
        if (n.nodeType === 1) log.push({ t: "remove", cls: n.className, cid: n.dataset && (n.dataset.cid || n.dataset.wid), txt: (n.textContent || "").slice(0, 40) });
      }
      if (m.type === "attributes") {
        log.push({ t: "attr:" + m.attributeName, target: m.target.className, cid: m.target.dataset && (m.target.dataset.cid || m.target.dataset.wid) });
      } else if (m.type === "characterData") {
        log.push({ t: "text", target: m.target.parentElement && m.target.parentElement.className, txt: String(m.target.textContent).slice(0, 40) });
      }
    }
  });
  mo.observe(tree, { childList: true, subtree: true, attributes: true, characterData: true, attributeOldValue: true });

  window.__navObs = {
    reset() { log.length = 0; netCounts.children = 0; netCounts.navFull = 0; netCounts.version = 0; recording = true; },
    pause() { recording = false; },
    // 行级快照：当前挂载的卡行/项目行 key 列表（V-R2 折叠卸载比对用）
    rows() {
      return {
        cards: [...tree.querySelectorAll(".nav-card")].map(el => el.dataset.cid),
        projects: [...tree.querySelectorAll(".nav-phead")].map(el => el.dataset.wid),
        count: tree.querySelectorAll(".nav-card,.nav-phead").length,
      };
    },
    domLog() { return log.slice(); },
    net() { return { ...netCounts }; },
    report() { return { net: { ...netCounts }, log: log.slice(), rows: this.rows() }; },
    destroy() { mo.disconnect(); window.fetch = origFetch; delete window.__navObs; },
  };
  console.info("[navObs] 就绪：reset() 开始记录 → 操作 → report() 取账");
})();

/* ============================== 判据操作序列 ==============================
 * V-R1（R-13 增量挂载）：
 *   __navObs.reset() → 点某有子卡节点的 ▸（data-twist）→ report()
 *   期望：log 只含该节点子行的 add 记录（+可能的 patch attr），其余行零触碰；
 *   再折叠同节点 → log 只含该子树行的 remove 记录。
 * V-R2（R-14 折叠即卸载）：
 *   reset() → rows() 存展开快照 → 折叠该节点 → rows() 与 DOM 检查：
 *   期望 qsa('.nav-card') 不含该子树任何行；重展后 rows() 与展开前快照一致。
 * V-R3（R-15 diff 只 patch 变化行）——需服务端数据变更，操作序列：
 *   reset() → （另端改某卡标题或等 60s 探测到版本变化）→ report()
 *   期望：log 只含变化行的 attr/text 记录，其余行零记录。
 *   ※ 服务端造数需写 works——本脚本不执行，真机验证时由用户操作或观察自然变化。
 * V-R4（R-19 悬停跳过）：
 *   reset() → 悬停某行（触发 temp.hover）→ 触发刷新（如切走再切回该卡）→ report()
 *   期望：该行无 attr/text 修改记录。
 * V-V4（R-22 网络计数）：
 *   reset() → 展开有子卡节点 → net().children === 1；
 *   同节点 pending 期间重复点 → 仍 1；折叠→重展 → 2；折叠→重展 → 3（每次都拉）；
 *   展开 child_count=0 的叶节点 → children 计数不增且行无 ▸（.nav-twist.none）。
 * V-V5（R-23 默认折叠）：
 *   localStorage.removeItem('nav.prefs.v1') → 强刷 → rows()
 *   期望：非当前项目仅项目行（无 .nav-card 属于它）；
 *   当前项目展开一层（根卡直系可见）；当前卡（hash 指向卡）祖先链逐层可见。
 * V-V6（探测替换，60s 周期）：
 *   reset() → 静置 60s+ → net()：version ≥1、navFull===0（无变化路径）；
 *   服务端有变化时：navFull===1 且只有变化 work 的行被 patch。
 *   ※ 变化路径机制已由 tests/s2/probe.test.js 单测闭合（V-V6c）。
 * ======================================================================== */
