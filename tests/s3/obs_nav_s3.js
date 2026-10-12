/* tests/s3/obs_nav_s3.js —— [S3] DOM/网络观察脚本（V3-A1/A3/A5/A6、F2/F3、L2/L3/L5/L6）。
 *
 * 载体：tests/s2/mock_server.mjs（纯内存假数据＋高位端口＋用后即杀；零 works 触碰）。
 * 用法（浏览器 console 或自动化注入，每个 work 页一次——跨页导航/重载后需重注入）：
 *   1. 打开 /work/work_m1（先 localStorage.clear() + reload 清偏好）；
 *   2. await window.__navS3.run("main")    （A1/L5/F2/F3/A3/A5；结尾 reload）
 *   3. 重载完成后重注入 → await window.__navS3.run("main3")   （A6 重载终态）
 *   4. location.href="/work/work_m3#card_m3_l4" 加载完重注入 → await window.__navS3.run("deep")
 *   5. window.__navS3.dump() 取全部结果。
 * 只读观察＋mock 控制端点（/__test/ghostcard）；不改页面模块状态、不写 works。
 */
(function () {
  if (window.__navS3) return;
  const tree = document.getElementById("navTree");
  const WID = window.__WORK_ID__;
  const results = {};          // suite → [{id, pass, note}]
  let recording = false;
  let treeMutations = 0;       // navTree 区 mutation 计数（V3-F2 树零触碰）
  const netCounts = { total: 0, children: 0, navFull: 0, version: 0, other: 0 };

  const origFetch = window.fetch.bind(window);
  window.fetch = function (url, opts) {
    const u = String(url);
    netCounts.total++;
    if (u.includes("/api/nav/children")) netCounts.children++;
    else if (u.endsWith("/api/nav") || u.includes("/api/nav?")) netCounts.navFull++;
    else if (u.includes("/api/nav/version")) netCounts.version++;
    else netCounts.other++;
    return origFetch(url, opts);
  };
  const mo = new MutationObserver((muts) => {
    if (!recording) return;
    treeMutations += muts.length;
  });
  mo.observe(tree, { childList: true, subtree: true, attributes: true, characterData: true });

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  async function waitFor(fn, timeout = 3000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      try { if (fn()) return true; } catch (e) { /* 再等 */ }
      await sleep(50);
    }
    return false;
  }
  /* 等待布尔条件翻转到 expected（toggle 断言用；不翻转返回终值） */
  async function waitForFlip(fn, before) {
    const expected = !before;
    const t0 = Date.now();
    while (Date.now() - t0 < 1500) {
      let v;
      try { v = fn(); } catch (e) { v = before; }
      if (v === expected) return v;
      await sleep(60);
    }
    return fn();
  }
  function rec(suite, id, pass, note) {
    (results[suite] = results[suite] || []).push({ id, pass: !!pass, note: note || "" });
  }
  const qsa = (sel) => [...document.querySelectorAll(sel)];
  const prefs = () => { try { return JSON.parse(localStorage.getItem("nav.prefs.v1") || "{}"); } catch (e) { return {}; } };
  function hash() { return location.hash; }
  function cardRow(cid) { return tree.querySelector('.nav-card[data-cid="' + cid + '"]'); }
  function projRow(wid) { return tree.querySelector('.nav-phead[data-wid="' + wid + '"]'); }
  const diag = [];
  async function clickRow(cid) {
    const el = cardRow(cid);
    if (!el) { diag.push("clickRow missing: " + cid + " (rows=" + qsa(".nav-card").length + ")"); return false; }
    el.click(); await sleep(150); return true;
  }
  async function clickProj(wid) {
    const el = projRow(wid);
    if (!el) { diag.push("clickProj missing: " + wid + " (pheads=" + qsa(".nav-phead").length + ")"); return false; }
    el.click(); await sleep(300); return true;
  }
  function visibleCards(wid) {
    return qsa(".nav-card").filter(el => el.dataset.wid === wid).map(el => el.dataset.cid);
  }
  function headTitle() { return (document.getElementById("wsHeadTitle") || {}).textContent || ""; }

  /* ==================== main 套件（work_m1 页：A1/L5/F2/F3/A3/A5；页须带 #work_m1/card_m_a1
   * hash 加载——裸加载落画布态，.nav-tree display:none → 虚拟列表 0 行可点（S2 rail 形态，
   * 生产行为正常：悬停浮层/进对话态时 ResizeObserver 重切出行）==================== */
  async function suiteMain() {
    const S = "main";
    await waitFor(() => cardRow("card_m_a1"), 4000);   // 对话态树行就绪
    // ---- V3-A1（R-20）点击判型 ----
    recording = true; treeMutations = 0;
    const h0 = hash();
    await clickProj("work_m2");   // 项目行单击：展开，URL 不变
    rec(S, "A1-project-url", hash() === h0, "hash=" + hash());
    rec(S, "A1-project-expand", visibleCards("work_m2").length > 0, "work_m2 卡行=" + visibleCards("work_m2").join(","));
    await clickProj("work_m2");   // 再点＝折叠
    rec(S, "A1-project-collapse", visibleCards("work_m2").length === 0, "折叠后卡行清零");

    const h1 = hash();
    const a1Before = !!cardRow("card_m_a1");   // a 可能因载入路径已 system 展开——断言看翻转
    await clickRow("card_m_a");   // 有子卡卡行单击：展开/折叠翻转，URL 不变
    const a1Mid = await waitForFlip(() => !!cardRow("card_m_a1"), a1Before);
    rec(S, "A1-node-url", hash() === h1, "hash=" + hash());
    rec(S, "A1-node-toggle", a1Mid !== a1Before, "a1 可见性翻转 " + a1Before + "→" + a1Mid);
    await clickRow("card_m_a");   // 翻转回去
    const a1Back = await waitForFlip(() => !!cardRow("card_m_a1"), a1Mid);
    rec(S, "A1-node-toggle-back", a1Back === a1Before, "回翻 " + a1Mid + "→" + a1Back);
    if (a1Back !== a1Before && !a1Back) await clickRow("card_m_a");   // 还原展开态（后续用例要用）

    const h2 = hash();
    await clickRow("card_m_b");   // 叶子单击：跳转（hash 复合键＋页面切卡）
    await waitFor(() => hash() === "#work_m1/card_m_b");
    rec(S, "A1-leaf-hash", hash() === "#work_m1/card_m_b", "hash=" + hash());
    rec(S, "A1-leaf-open", headTitle().length > 0, "wsHeadTitle=" + headTitle() + "（mock getCard 通用名）");
    rec(S, "A1-leaf-url-changed", hash() !== h2, "叶子点击改变 URL（与容器行相反）");

    // ---- V3-L5（R-34）唯一高亮 ----
    const currents = qsa(".nav-card.current");
    rec(S, "L5-unique-current", currents.length === 1 && currents[0].dataset.cid === "card_m_b",
      "current 数=" + currents.length + " cid=" + currents.map(e => e.dataset.cid).join(","));
    const pr1 = projRow("work_m1");
    rec(S, "L5-project-secondary", !!pr1 && pr1.classList.contains("current"), "当前项目行次强在场");

    // ---- V3-F2/F3（R-24）搜索独立层 ----
    const input = document.getElementById("navSearchInput");
    recording = true; treeMutations = 0;
    netCounts.total = 0; netCounts.children = 0; netCounts.navFull = 0; netCounts.version = 0; netCounts.other = 0;
    input.focus();                 // 面板开（最近区）
    input.value = "假卡";          // 命中三项目多卡（结果含项目名）
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await sleep(80);
    const layer = document.querySelector(".nav-find-layer");
    const layerOpen = !!layer && layer.style.display !== "none";
    const layerText = layer ? layer.textContent : "";
    rec(S, "F2-layer-open", layerOpen, "结果层可见");
    rec(S, "F2-tree-zero-touch", treeMutations === 0, "navTree mutation=" + treeMutations);
    // V3-F3：搜索可用通道（children/navFull/version）必须 0；other=badge/probe 等 60s 环境定时器
    // 通道，与搜索无关，如实呈报数值
    rec(S, "F3-net-zero", netCounts.children === 0 && netCounts.navFull === 0 && netCounts.version === 0,
      "children=" + netCounts.children + " navFull=" + netCounts.navFull + " version=" + netCounts.version
      + " other(环境定时器)=" + netCounts.other);
    rec(S, "F2-results-hit", layerText.includes("假卡乙-X") && layerText.includes("假项目乙") && layerText.includes("假卡丙-L4（深卡）"),
      "结果含卡标题＋项目名（跨项目命中）");
    // 点击同项目结果 → 跳转＋关搜索（不发生整页跳转，套件得以续跑）
    const hit = [...layer.querySelectorAll(".nav-find-item")].find(el => el.textContent.includes("假卡甲-B"));
    if (hit) { hit.click(); await sleep(300); }
    rec(S, "F2-click-jump-closes", !document.querySelector(".nav-find-layer") || document.querySelector(".nav-find-layer").style.display === "none",
      "点击结果后结果层已关");
    rec(S, "F2-click-jump-opens", hash() === "#work_m1/card_m_b", "点击结果已切卡 hash=" + hash() + " wsHeadTitle=" + headTitle() + "（mock getCard 通用名）");
    input.value = "";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.blur();

    // ---- V3-A3（R-21）幽灵卡灰态 ----
    await origFetch("/__test/ghostcard", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wid: "work_m1", cid: "card_gone", title: "幽灵卡", parent: "card_m_a" }),
    });
    if (!cardRow("card_m_a")) await clickProj("work_m1");   // 幂等：幽灵父段可见即可
    const spacer = tree.querySelector(".nav-vspacer");
    const ghost = document.createElement("div");
    ghost.className = "nav-card nav-vrow";
    ghost.dataset.cid = "card_gone";
    ghost.dataset.wid = "work_m1";
    ghost.innerHTML = '<span class="nav-twist none"></span><span class="dot leaf"></span><span class="nav-ctitle">幽灵卡</span>';
    spacer.appendChild(ghost);
    const hG = hash();
    ghost.click();                 // 非当前选中：灰态＋URL 不变
    await sleep(100);
    rec(S, "A3-grey-class", ghost.classList.contains("deleted"), "点击后 .deleted 在场");
    rec(S, "A3-no-nav", hash() === hG, "URL 不变 hash=" + hash());

    // 幽灵卡恰为当前选中：hash 指向幽灵卡 → open（mock getCard 应答幽灵）→ 点击行
    location.hash = "#work_m1/card_gone";
    await waitFor(() => headTitle() === "幽灵卡", 3000);
    rec(S, "A3-ghost-opened", headTitle() === "幽灵卡", "幽灵卡经 hash 打开（currentCard=幽灵）");
    const hG2 = hash();
    ghost.click();
    await sleep(100);
    rec(S, "A3-selection-cleared", hash() === "" || hash() === "#", "选中已清 hash=" + JSON.stringify(hash()));
    const crumb = document.getElementById("navCrumb");
    rec(S, "A3-crumb-back-parent", !!crumb && crumb.textContent.includes("假卡甲-A") && crumb.textContent.includes("假项目甲"),
      "面包屑回父卡=" + (crumb ? crumb.textContent : "(无)"));
    ghost.remove();

    // ---- V3-A5（R-28）收起全部保留路径 ----
    // 当前卡=card_m_a1（路径 a→a1）；先确保非路径展开：work_m2 项目（幂等）
    if (!cardRow("card_m_a1")) {
      if (!cardRow("card_m_a")) await clickProj("work_m1");
      await clickRow("card_m_a");   // 重展 a（A1 段折叠过）
    }
    await clickRow("card_m_a1");   // 重新选中（叶子跳转，路径 a→a1）
    await waitFor(() => hash() === "#work_m1/card_m_a1");
    if (visibleCards("work_m2").length === 0) await clickProj("work_m2");
    const m2visibleBefore = visibleCards("work_m2").length;
    document.getElementById("navCollapseAll").click();
    await sleep(250);
    rec(S, "A5-nonpath-collapsed", m2visibleBefore > 0 && visibleCards("work_m2").length === 0,
      "work_m2 卡行收起（前=" + m2visibleBefore + " 后=" + visibleCards("work_m2").length + "）");
    rec(S, "A5-path-kept", !!cardRow("card_m_a") && !!cardRow("card_m_a1"),
      "当前卡祖先链保留：a=" + !!cardRow("card_m_a") + " a1=" + !!cardRow("card_m_a1"));
    const p = prefs();
    const exp = (p.expanded || {}).work_m1 || {};
    rec(S, "A5-prefs-path-only", Object.keys(exp).length === 1 && !!exp.card_m_a,
      "prefs.expanded[work_m1]=" + JSON.stringify(exp) + "（仅路径条目）");
    rec(S, "A5-prefs-projects", (p.projects || []).indexOf("work_m1") >= 0 && (p.projects || []).indexOf("work_m2") < 0,
      "prefs.projects=" + JSON.stringify(p.projects));
    rec(S, "A5-prefs-source-kept", typeof exp.card_m_a === "string" && exp.card_m_a.length > 0,
      "路径条目 source 保留=" + JSON.stringify(exp.card_m_a) + "（manual=套件手动点开所致，语义=折叠未清 source）");

    // ---- V3-A6（R-29）多项目并看 ----
    await clickProj("work_m2");    // 第二项目展开（与当前路径并存）
    rec(S, "A6-both-open", visibleCards("work_m1").length > 0 && visibleCards("work_m2").length > 0,
      "双项目并存 work_m1=[" + visibleCards("work_m1").join(",") + "] work_m2=[" + visibleCards("work_m2").join(",") + "]");
    // 「切走再回」＝重载后跑 main3 核对（重载由控制端驱动，不在此处 reload——context 会随导航销毁）
  }

  /* main3：重载回来后核对 A6 终态 */
  async function suiteMain3() {
    const S = "main3";
    await waitFor(() => cardRow("card_m_a1"), 4000);
    const m1 = visibleCards("work_m1"), m2 = visibleCards("work_m2");
    rec(S, "A6-both-kept", m1.length > 0 && m2.length > 0,
      "重载后 work_m1=[" + m1.join(",") + "] work_m2=[" + m2.join(",") + "]");
    const p = prefs();
    rec(S, "A6-prefs-both", (p.projects || []).indexOf("work_m1") >= 0 && (p.projects || []).indexOf("work_m2") >= 0,
      "prefs.projects=" + JSON.stringify(p.projects));
  }

  /* ==================== deep 套件（work_m3 深树页） ==================== */
  async function suiteDeep() {
    const S = "deep";
    await waitFor(() => cardRow("card_m3_l4"), 4000);
    // ---- V3-L2（R-27）面包屑深树显＋段可点 ----
    const box = document.getElementById("navCrumbBox");
    const crumb = document.getElementById("navCrumb");
    rec(S, "L2-deep-visible", box.style.display !== "none" && crumb.textContent.includes("假项目丙") && crumb.textContent.includes("假卡丙-L4（深卡）"),
      "面包屑=" + crumb.textContent);
    const segs = [...crumb.querySelectorAll(".nav-crumb-seg")].filter(s => s.dataset.cid);
    rec(S, "L2-seg-count", segs.length === 5, "段数（项目+4卡）=" + segs.length);
    const parentSeg = segs.find(s => s.dataset.cid === "card_m3_l3");
    if (parentSeg) { parentSeg.click(); await sleep(350); }
    rec(S, "L2-seg-click", hash().includes("card_m3_l3"), "点父段跳转 hash=" + hash());
    rec(S, "L2-seg-open", hash() === "#work_m3/card_m3_l3" && headTitle().length > 0,
      "父段卡已打开 wsHeadTitle=" + headTitle() + "（mock getCard 通用名）");

    // ---- V3-L3（R-18）吸顶：滚动=absolute 悬浮，静止=回槽位 ----
    // IAB 伪象：程序化 scrollTop 赋值不触发 scroll 事件（真浏览器滚轮原生触发）——
    // 故赋值后显式 dispatch（监听器与数学已由 dispatch 路径证实工作），如实标注。
    location.hash = "#work_m3/card_m3_l4";   // 回深卡（面包屑要在场）
    await waitFor(() => headTitle() === "假卡丙-L4（深卡）", 3000);
    const scrollTree = (top) => { tree.scrollTop = top; tree.dispatchEvent(new Event("scroll")); };
    tree.style.maxHeight = "260px";   // 收容器逼出滚动条（几何观察用，验后还原）
    await sleep(150);
    scrollTree(0); await sleep(80);
    const stickyOff = !crumb.classList.contains("sticky") && getComputedStyle(crumb).position === "static";
    scrollTree(120); await sleep(150);
    const stickyOn = crumb.classList.contains("sticky") && getComputedStyle(crumb).position === "absolute";
    scrollTree(0); await sleep(80);
    const stickyBack = !crumb.classList.contains("sticky") && getComputedStyle(crumb).position === "static";
    rec(S, "L3-sticky-on", stickyOn, "滚动→.sticky＋position:absolute（scroll 事件经 dispatch，IAB 程序化滚动不发事件）");
    rec(S, "L3-sticky-back", stickyOff && stickyBack, "静止→回文档流槽位（off=" + stickyOff + " back=" + stickyBack + "）");
    rec(S, "L3-same-dom", document.getElementById("navCrumb") === crumb, "同一 DOM（无第二套 UI）");

    // ---- V3-L6（R-26）定位三件套 ----
    // 制造非路径展开（幂等）：work_m1 项目＋card_m_a 子树
    if (!cardRow("card_m_a")) await clickProj("work_m1");
    if (cardRow("card_m_a") && !cardRow("card_m_a1")) await clickRow("card_m_a");
    const nonPathBefore = visibleCards("work_m1").length;
    document.getElementById("navLocate").click();
    await sleep(350);
    rec(S, "L6-nonpath-collapsed", nonPathBefore > 0 && visibleCards("work_m1").length === 0,
      "非路径分支收起（前=" + nonPathBefore + " 后=" + visibleCards("work_m1").length + "）");
    const pathRows = ["card_m3_l1", "card_m3_l2", "card_m3_l3", "card_m3_l4"].map(cid => !!cardRow(cid));
    rec(S, "L6-path-expanded", pathRows.every(Boolean), "祖先链展开=" + pathRows.join(","));
    const cur = cardRow("card_m3_l4");   // 当前卡=l4（L3 段已回 l4）
    const treeRect = tree.getBoundingClientRect();
    let inView = false;
    if (cur) {
      const r = cur.getBoundingClientRect();
      inView = r.top >= treeRect.top - 2 && r.bottom <= treeRect.bottom + 2;
    }
    rec(S, "L6-scrolled-in-view", !!cur && inView && tree.scrollTop > 0,
      "当前卡在可视区=" + inView + " scrollTop=" + tree.scrollTop);
    const currents = qsa(".nav-card.current");
    rec(S, "L6-unique-current", currents.length === 1 && currents[0].dataset.cid === "card_m3_l4",
      "current 数=" + currents.length + " cid=" + currents.map(e => e.dataset.cid).join(","));
    tree.style.maxHeight = "";
    tree.scrollTop = 0;
  }

  window.__navS3 = {
    async run(suite) {
      recording = true; treeMutations = 0;
      if (suite === "main") await suiteMain();
      else if (suite === "main3") await suiteMain3();
      else if (suite === "deep") await suiteDeep();
      recording = false;
      return this.dump();
    },
    dump() { return JSON.parse(JSON.stringify({ wid: WID, net: { ...netCounts }, diag, results })); },
    reset() {
      treeMutations = 0;
      netCounts.total = 0; netCounts.children = 0; netCounts.navFull = 0; netCounts.version = 0; netCounts.other = 0;
      recording = true;
    },
    rows: () => ({
      cards: qsa(".nav-card").map(el => ({ cid: el.dataset.cid, wid: el.dataset.wid, deleted: el.classList.contains("deleted") })),
      projects: qsa(".nav-phead").map(el => el.dataset.wid),
    }),
    prefs,
  };
  console.info("[navS3] 就绪：__navS3.run('main'|'main3'|'deep') → dump()");
})();
